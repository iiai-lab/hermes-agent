"""Local and API-server turns run ``pre_gateway_dispatch`` before admission.

Messaging already fires the hook. A Desktop/CLI/TUI prompt and a
``/v1/chat/completions`` turn did not, so a session-reset policy never stamped
or reset those lanes. These tests are the contract for that call site.
"""
import json
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.event import MessageEvent
from gateway.run_inbound_hooks import GatewayInboundHooksMixin
from gateway.session import SessionSource, build_session_key
from gateway.session_contract import SessionRef


def _bind_lane_hook(runner):
    runner.pre_gateway_dispatch_lane_hook = types.MethodType(
        GatewayInboundHooksMixin.pre_gateway_dispatch_lane_hook, runner)
    runner._hm_pre_gateway_dispatch_hook = types.MethodType(
        GatewayInboundHooksMixin._hm_pre_gateway_dispatch_hook, runner)


def _key_for(source):
    return build_session_key(source)


class _RouteStore:
    """The slice of SessionStore the lane and a fake policy touch."""

    def __init__(self):
        self.entries = {}
        self.meta = {}

    def lookup_by_session_key(self, key):
        return self.entries.get(key)

    def set_session_metadata(self, key, meta_key, value):
        self.meta[(key, meta_key)] = value
        entry = self.entries.get(key)
        if entry is not None:
            entry.metadata[meta_key] = value
        return True

    def _generate_session_key(self, source):
        return build_session_key(source)


def _entry(session_id):
    return SimpleNamespace(session_id=session_id, metadata={})


@dataclass
class _Receipt:
    status: str = "queued"
    admission_id: str = "adm"


def _local_connection(source, *, hook=True, store=None):
    from gateway.session_controls import AuthorityConnection

    runner = SimpleNamespace(session_store=store or _RouteStore())
    runner._session_key_for_source = _key_for
    if hook:
        _bind_lane_hook(runner)
    submitted = []

    async def _submit(actor, request, **kwargs):
        submitted.append(request)
        return _Receipt()

    authority = SimpleNamespace(
        instance_id="owner", profile_id="owned", events={}, runner=runner,
        sessions={"s": SimpleNamespace(source=source)}, submit=_submit, authorize=lambda *a, **k: None)
    connection = AuthorityConnection(authority, object(), {"user_id": "human"})
    connection.subscriptions["s"] = "sub"
    return connection, submitted, runner


def _ref():
    return SessionRef("owned", "s")


def _local_source():
    return SessionSource(platform=Platform.LOCAL, chat_id="local-chat", user_id="human", chat_type="dm")


@pytest.mark.asyncio
async def test_local_prompt_passes_the_lane_hook_once_with_text_and_source(monkeypatch):
    seen = []

    async def _hook(name, **kwargs):
        seen.append((name, kwargs["event"].text, kwargs["event"].source))
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(_local_source())
    result = await connection.submit(_ref(), {"text": "good morning", "submission_id": "p1"})
    assert result["status"] == "queued"
    assert len(submitted) == 1
    assert seen == [("pre_gateway_dispatch", "good morning", connection.authority.sessions["s"].source)]
    assert isinstance(seen[0][2], SessionSource)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"submission_id": "p1"},
    {"text": "   ", "submission_id": "p1"},
    {"text": "hidden note", "display_kind": "hidden", "submission_id": "p1"},
])
async def test_local_non_conversation_submit_skips_the_lane_hook(monkeypatch, params):
    called = []

    async def _hook(name, **kwargs):
        called.append(name)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(_local_source())
    await connection.submit(_ref(), params)
    assert submitted and called == []


@pytest.mark.asyncio
async def test_non_local_submit_skips_the_lane_hook(monkeypatch):
    called = []

    async def _hook(name, **kwargs):
        called.append(name)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="human", chat_type="dm")
    connection, submitted, _runner = _local_connection(source)
    await connection.submit(_ref(), {"text": "hello", "submission_id": "p1"})
    assert submitted and called == []


@pytest.mark.asyncio
async def test_local_submit_without_a_lane_hook_keeps_legacy_dispatch(monkeypatch):
    called = []

    async def _hook(name, **kwargs):
        called.append(name)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(_local_source(), hook=False)
    await connection.submit(_ref(), {"text": "hello", "submission_id": "p1"})
    assert submitted and called == []

    # An auto-grown attribute must not count. A MagicMock runner would otherwise
    # answer the name on access.
    magic = _local_connection(_local_source(), hook=False)
    from gateway.session_controls import AuthorityConnection
    authority = SimpleNamespace(
        instance_id="owner", profile_id="owned", events={}, runner=MagicMock(),
        sessions={"s": SimpleNamespace(source=_local_source())},
        submit=magic[0].authority.submit, authorize=lambda *a, **k: None)
    bare = AuthorityConnection(authority, object(), {"user_id": "human"})
    bare.subscriptions["s"] = "sub"
    await bare.submit(_ref(), {"text": "hello", "submission_id": "p2"})
    authority.runner.pre_gateway_dispatch_lane_hook.assert_not_called()


@pytest.mark.asyncio
async def test_local_skip_refuses_before_commit(monkeypatch):
    async def _hook(name, **kwargs):
        return [{"action": "skip"}]

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(_local_source())
    with pytest.raises(Exception) as raised:
        await connection.submit(_ref(), {"text": "drop me", "submission_id": "p1"})
    assert getattr(raised.value, "reason", None) == "permission_denied"
    assert submitted == []


@pytest.mark.asyncio
async def test_local_hook_exception_fail_opens(monkeypatch, caplog):
    async def _hook(name, **kwargs):
        raise RuntimeError("plugin broke")

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(_local_source())
    with caplog.at_level("WARNING", logger="gateway.run"):
        result = await connection.submit(_ref(), {"text": "still goes", "submission_id": "p1"})
    assert result["status"] == "queued"
    assert len(submitted) == 1
    assert any("pre_gateway_dispatch invocation failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_local_stamp_lands_on_the_route_entry(monkeypatch):
    store = _RouteStore()
    source = _local_source()
    key = build_session_key(source)
    store.entries[key] = _entry("local-1")

    async def _hook(name, **kwargs):
        gateway = kwargs["gateway"]
        route = gateway._session_key_for_source(kwargs["event"].source)
        kwargs["session_store"].set_session_metadata(route, "srp_last_user_inbound", "2026-11-26T04:00:00")
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _hook)
    connection, submitted, _runner = _local_connection(source, store=store)
    await connection.submit(_ref(), {"text": "still today", "submission_id": "p1"})
    assert submitted
    assert store.meta[(key, "srp_last_user_inbound")] == "2026-11-26T04:00:00"
    assert key.startswith("agent:main:local:")


def _adapter(api_key=""):
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": api_key} if api_key else {}))


def _app(adapter):
    from aiohttp import web
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _runner(store, plugin):
    runner = SimpleNamespace(session_store=store, _plugin=plugin)
    runner._session_key_for_source = _key_for
    _bind_lane_hook(runner)

    async def _hook(name, **kwargs):
        return await plugin(name, **kwargs)

    return runner, _hook


async def _post_chat(adapter, body, headers=None):
    """``(status, headers, json)``. The body is read before the test client closes."""
    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", json=body, headers=headers or {})
        payload = await response.json()
        return response.status, response.headers, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("with_header", [False, True])
async def test_api_chat_passes_the_lane_hook_once_before_dispatch(monkeypatch, with_header):
    store = _RouteStore()
    order = []
    seen = []

    async def _plugin(name, **kwargs):
        order.append("hook")
        event = kwargs["event"]
        seen.append((event.text, event.source.platform, event.source.chat_id, event.source.chat_type, event.source.user_id))
        return []

    runner, hook = _runner(store, _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter("sk-secret" if with_header else "")
    adapter.gateway_runner = runner

    async def _no_store():
        return None

    adapter._ensure_store_async = _no_store

    async def _run_agent(**kwargs):
        order.append("run")
        return {"final_response": "ok", "session_id": kwargs["session_id"]}, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    adapter._run_agent = _run_agent
    headers = {"Authorization": "Bearer sk-secret", "X-Hermes-Session-Id": "api-header"} if with_header else {}
    status, _headers, payload = await _post_chat(
        adapter, {"model": "m", "messages": [{"role": "user", "content": "good morning"}]}, headers)
    assert status == 200, payload
    assert order == ["hook", "run"]
    text, platform, chat_id, chat_type, user_id = seen[0]
    assert text == "good morning"
    assert platform == Platform.API_SERVER and chat_type == "dm" and user_id == "api"
    if with_header:
        assert chat_id == "api-header"
    else:
        assert chat_id
    assert build_session_key(SessionSource(
        platform=Platform.API_SERVER, chat_id=chat_id, user_id="api", chat_type="dm")).startswith(
            "agent:main:api_server:dm:")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_header", [False, True])
async def test_api_reset_lands_on_the_route_tip(monkeypatch, with_header):
    from gateway.platforms.api_server import _derive_chat_session_id

    store = _RouteStore()
    body = {"model": "m", "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "good morning"}]}
    if with_header:
        origin = "api-header"
    else:
        origin = _derive_chat_session_id("sys", "good morning", None)
    key = build_session_key(SessionSource(
        platform=Platform.API_SERVER, chat_id=origin, user_id="api", chat_type="dm"))
    store.entries[key] = _entry(origin)

    async def _plugin(name, **kwargs):
        route = kwargs["gateway"]._session_key_for_source(kwargs["event"].source)
        kwargs["session_store"].lookup_by_session_key(route).session_id = "tip-session"
        return []

    runner, hook = _runner(store, _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter("sk-secret" if with_header else "")
    adapter.gateway_runner = runner

    async def _no_store():
        return None

    adapter._ensure_store_async = _no_store
    captured = {}

    async def _run_agent(**kwargs):
        captured["session_id"] = kwargs["session_id"]
        captured["history"] = kwargs["conversation_history"]
        return {"final_response": "ok", "session_id": kwargs["session_id"]}, {}

    adapter._run_agent = _run_agent
    headers = {"Authorization": "Bearer sk-secret", "X-Hermes-Session-Id": "api-header"} if with_header else {}
    status, headers_out, payload = await _post_chat(adapter, body, headers)
    assert status == 200, payload
    assert captured["session_id"] == "tip-session"
    assert headers_out["X-Hermes-Session-Id"] == "tip-session"
    if with_header:
        assert captured["history"] == []


@pytest.mark.asyncio
async def test_api_stamp_lands_on_the_route_entry(monkeypatch):
    from gateway.platforms.api_server import _derive_chat_session_id

    store = _RouteStore()
    origin = _derive_chat_session_id("", "still today", None)
    key = build_session_key(SessionSource(
        platform=Platform.API_SERVER, chat_id=origin, user_id="api", chat_type="dm"))
    store.entries[key] = _entry(origin)

    async def _plugin(name, **kwargs):
        route = kwargs["gateway"]._session_key_for_source(kwargs["event"].source)
        kwargs["session_store"].set_session_metadata(route, "srp_last_user_inbound", "2026-11-26T04:10:00")
        return []

    runner, hook = _runner(store, _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter()
    adapter.gateway_runner = runner

    async def _run_agent(**kwargs):
        return {"final_response": "ok", "session_id": kwargs["session_id"]}, {}

    adapter._run_agent = _run_agent
    status, _headers, payload = await _post_chat(
        adapter, {"model": "m", "messages": [{"role": "user", "content": "still today"}]})
    assert status == 200, payload
    assert store.entries[key].metadata["srp_last_user_inbound"] == "2026-11-26T04:10:00"
    assert key.startswith("agent:main:api_server:dm:")


@pytest.mark.asyncio
async def test_api_skip_is_a_4xx_and_does_not_dispatch(monkeypatch):
    async def _plugin(name, **kwargs):
        return [{"action": "skip", "reason": "policy"}]

    runner, hook = _runner(_RouteStore(), _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter()
    adapter.gateway_runner = runner
    called = []

    async def _run_agent(**kwargs):
        called.append(kwargs)
        return {"final_response": "ok"}, {}

    adapter._run_agent = _run_agent
    status, _headers, payload = await _post_chat(
        adapter, {"model": "m", "messages": [{"role": "user", "content": "drop me"}]})
    assert 400 <= status < 500
    assert called == []
    assert payload["error"]["code"] == "pre_gateway_dispatch_skip"


@pytest.mark.asyncio
async def test_api_hook_exception_fail_opens(monkeypatch, caplog):
    async def _plugin(name, **kwargs):
        raise RuntimeError("plugin broke")

    runner, hook = _runner(_RouteStore(), _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter()
    adapter.gateway_runner = runner
    called = []

    async def _run_agent(**kwargs):
        called.append(kwargs["session_id"])
        return {"final_response": "ok", "session_id": kwargs["session_id"]}, {}

    adapter._run_agent = _run_agent
    with caplog.at_level("WARNING", logger="gateway.run"):
        status, _headers, payload = await _post_chat(
            adapter, {"model": "m", "messages": [{"role": "user", "content": "still goes"}]})
    assert status == 200, payload
    assert called
    assert any("pre_gateway_dispatch invocation failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_api_without_a_lane_hook_keeps_legacy_dispatch(monkeypatch):
    called = []

    async def _plugin(name, **kwargs):
        called.append(name)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _plugin)
    adapter = _adapter()
    # A runner from before this entry existed: no lane hook, and no session authority
    # (a MagicMock runner would invent one and change session resolution itself).
    adapter.gateway_runner = SimpleNamespace(session_store=_RouteStore())

    async def _run_agent(**kwargs):
        return {"final_response": "ok", "session_id": kwargs["session_id"]}, {}

    adapter._run_agent = _run_agent
    status, _headers, payload = await _post_chat(
        adapter, {"model": "m", "messages": [{"role": "user", "content": "hello"}]})
    assert status == 200, payload
    assert called == []


@pytest.mark.asyncio
async def test_api_compression_lineage_hooks_the_origin_route_key(monkeypatch):
    """A header already resolved to a compression tip still keys the lane by the origin.

    The routing entry stays on ``dm:<origin>`` and only its ``session_id`` is the
    tip. The plugin must see that key, the stamp must land on it, a reset must
    adopt the new id, and a compression-only turn must still echo the client id.
    """
    from gateway.platforms.api_server_openai_routes import _echo_chat_session_id

    origin = "api-header"
    tip = "comp-tip-1"
    store = _RouteStore()
    key = build_session_key(SessionSource(
        platform=Platform.API_SERVER, chat_id=origin, user_id="api", chat_type="dm"))
    tip_key = build_session_key(SessionSource(
        platform=Platform.API_SERVER, chat_id=tip, user_id="api", chat_type="dm"))
    store.entries[key] = _entry(tip)
    seen = []
    reset = {"on": False}

    async def _plugin(name, **kwargs):
        route = kwargs["gateway"]._session_key_for_source(kwargs["event"].source)
        seen.append(route)
        if reset["on"]:
            kwargs["session_store"].lookup_by_session_key(route).session_id = "reset-child"
            return []
        kwargs["session_store"].set_session_metadata(route, "srp_last_user_inbound", "2026-11-26T06:00:00")
        return []

    runner, hook = _runner(store, _plugin)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    adapter = _adapter("sk-secret")
    adapter.gateway_runner = runner
    reloaded = []

    async def _reload(session_id):
        reloaded.append(session_id)
        return [{"role": "user", "content": "from-new"}]

    adapter._reload_chat_history = _reload
    prior = [{"role": "user", "content": "earlier"}]
    landed, history, moved, dropped = await adapter._apply_chat_lane_policy(tip, prior, "hello", origin)
    assert dropped is None
    assert seen == [key]
    assert key != tip_key
    assert tip_key not in store.entries
    assert store.entries[key].metadata["srp_last_user_inbound"] == "2026-11-26T06:00:00"
    assert store.meta[(key, "srp_last_user_inbound")] == "2026-11-26T06:00:00"
    assert landed == tip and moved is False and history is prior and reloaded == []
    # #13437: the turn runs on the tip, the response still names the id the client sent.
    assert _echo_chat_session_id(origin, landed, policy_moved=moved, result_session=tip) == origin

    reset["on"] = True
    landed, history, moved, dropped = await adapter._apply_chat_lane_policy(
        tip, list(prior), "hello again", origin)
    assert dropped is None
    assert seen[-1] == key
    assert landed == "reset-child" and moved is True
    assert reloaded == ["reset-child"]
    assert history == [{"role": "user", "content": "from-new"}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("authority_cwd_in_tmp")
async def test_local_reset_prompt_and_a_later_viewer_follow_the_receipt_tip(tmp_path, monkeypatch):
    """A policy reset advances the local receipt. The prompt and a stale viewer follow that tip."""
    import pm.environments as pm_env
    # This worktree sits under the real Hermes home, and the directory beside it
    # holds an Orca manifest. Importing the runner asks PM for that file; the
    # home guard then refuses the read. The runner only needs a home path here.
    monkeypatch.setattr(pm_env, "_payload_manifest", lambda root: None)
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local_recovery import transcript_target
    from tests.gateway.fixtures.storage_handles import storage_handle

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"model": {"default": "m"}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    store = SessionStore(home / "sessions", GatewayConfig())
    runner = SimpleNamespace(
        session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
        _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
        _peek_session_state=lambda key: None)

    async def _handle_message(event):
        return "ok"

    runner._handle_message = _handle_message
    runner._adapter_for_source = lambda source: None
    runner._session_key_for_source = lambda source: store._generate_session_key(source)
    _bind_lane_hook(runner)
    persistence = storage_handle(home)
    authority = SessionAuthority(
        runner, profile_id="owned", instance_id="owner", persistence=persistence,
        epoch=persistence.runtime.begin_epoch(instance_id="owner"))
    runner.session_authority = authority
    owner = AuthorityConnection(authority, object(), {"user_id": "human"})
    try:
        created = await owner.dispatch({
            "id": "create", "method": "session.create",
            "params": {"request_id": "chat", "source": "cli", "cwd": str(tmp_path), "toolsets": [], "model": "m"}})
        assert "result" in created, created
        sid = created["result"]["session_id"]
        resumed = await owner.dispatch({"id": "resume", "method": "session.resume", "params": {"session_id": sid}})
        assert "result" in resumed, resumed
        live = authority.sessions[sid]
        # The policy resolves the key the gateway helper returns. That is the live route.
        runner._session_key_for_source = lambda source, route=live.route: route
        child = "reset-child"

        async def _reset_plugin(name, **kwargs):
            if name != "pre_gateway_dispatch":
                return []
            # The same write ``SessionStore.reset_session`` does for a local route
            # (``bindings.reset_target``): the parent ends ``session_reset`` and the
            # receipt tip moves. This fixture's store has no authority epoch, so the
            # plugin calls the binding directly, as the local reset tests do.
            from gateway.session import SessionEntry
            store._record_gateway_session_peer(sid, live.route, live.source)
            old = SessionEntry.from_dict(authority.persistence.bindings.receipt(sid).entry)
            entry = SessionEntry(
                old.session_key, child, old.created_at, old.updated_at, origin=old.origin,
                platform=old.platform, chat_type=old.chat_type, is_fresh_reset=True)
            authority.persistence.bindings.reset_target(
                epoch=authority.epoch, parent_session_id=sid, entry=entry.to_dict())
            store._entries[live.route] = entry
            return []

        monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _reset_plugin)
        submitted = await owner.dispatch({
            "id": "prompt", "method": "prompt.submit",
            "params": {"session_id": sid, "submission_id": "morning", "text": "good morning"}})
        assert "result" in submitted, submitted
        tip = transcript_target(authority, SessionRef(authority.profile_id, sid))
        assert tip != sid

        async def _stamp_plugin(name, **kwargs):
            if name != "pre_gateway_dispatch":
                return []
            key = kwargs["gateway"]._session_key_for_source(kwargs["event"].source)
            kwargs["session_store"].set_session_metadata(key, "srp_last_user_inbound", "2026-11-26T05:00:00")
            return []

        stale = AuthorityConnection(authority, object(), {"user_id": "human"})
        try:
            again = await stale.dispatch({
                "id": "resume2", "method": "session.resume", "params": {"session_id": sid}})
            assert "result" in again, again
            monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", _stamp_plugin)
            follow = await stale.dispatch({
                "id": "prompt2", "method": "prompt.submit",
                "params": {"session_id": sid, "submission_id": "later", "text": "still the new chat"}})
            assert "result" in follow, follow
            assert transcript_target(authority, SessionRef(authority.profile_id, sid)) == tip
            assert store.get_session_metadata(live.route, "srp_last_user_inbound") == "2026-11-26T05:00:00"
        finally:
            await stale.close()
    finally:
        await owner.close()
        store.close_all_db_handles()
