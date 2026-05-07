"""Tests for read-only tmux TUI observation helpers."""

import pytest

from hermes_cli import tui_observation


def test_validate_pane_id_accepts_percent_number():
    assert tui_observation.validate_pane_id("%12") == "%12"


@pytest.mark.parametrize("pane_id", ["", "12", "%", "%abc", "%1;rm -rf /", "session:0.1"])
def test_validate_pane_id_rejects_tmux_target_strings(pane_id):
    with pytest.raises(ValueError):
        tui_observation.validate_pane_id(pane_id)


def test_parse_tmux_list_panes_contract():
    raw = "\t".join(
        [
            "work",
            "0",
            "claude",
            "1",
            "%12",
            "1",
            "claude",
            "Claude Code",
            "120",
            "40",
            "12345",
            "/repo",
        ]
    )

    sessions = tui_observation.parse_tmux_panes(raw)

    assert sessions == [
        {
            "id": "%12",
            "pane_id": "%12",
            "session_name": "work",
            "window_index": 0,
            "window_name": "claude",
            "pane_index": 1,
            "active": True,
            "command": "claude",
            "title": "Claude Code",
            "pane_title": "Claude Code",
            "width": 120,
            "height": 40,
            "pid": 12345,
            "cwd": "/repo",
            "current_path": "/repo",
            "dead": False,
            "agent_kind": "claude-code",
            "label": "work:0.claude:%12",
            "status": "possibly_idle",
            "reason": "known_agent_detected",
            "confidence": 0.35,
            "evidence": ["command=claude", "title=Claude Code"],
            "attach_command": "tmux attach-session -t work",
            "capture_command": "tmux capture-pane -p -t %12",
        }
    ]


def test_parse_tmux_list_panes_redacts_sensitive_metadata():
    raw = "\t".join(
        [
            "work-GITHUB_TOKEN=opaque-token-value",
            "0",
            "codex",
            "1",
            "%12",
            "1",
            "codex",
            "Authorization: Bearer opaque-access-token",
            "120",
            "40",
            "12345",
            "/repo?client_secret=opaque-secret&access_token=opaque-token",
        ]
    )

    session = tui_observation.parse_tmux_panes(raw)[0]
    encoded = str(session)

    assert "opaque-token-value" not in encoded
    assert "opaque-access-token" not in encoded
    assert "opaque-secret" not in encoded
    assert "opaque-token" not in encoded
    assert "GITHUB_TOKEN" not in encoded
    assert session["session_name"] == "[REDACTED]"
    assert session["pane_title"] == "[REDACTED]"


def test_redact_observation_text_masks_common_tokens():
    raw_openai_key = "sk-proj-" + "A" * 40
    raw_bearer = "bearer-token-" + "B" * 32
    text = "\n".join(
        [
            f"OPENAI_API_KEY={raw_openai_key}",
            "OPENAI_API_KEY=opaque-secret-value",
            '{"api_key": "opaque-json-secret"}',
            f"standalone {raw_openai_key}",
            f"Authorization: Bearer {raw_bearer}",
            "https://example.test/callback?access_token=opaque-url-token&client_secret=opaque-secret",
            "api_key=opaque-lower-api-key",
            "token: opaque-colon-token",
            "password = opaque-password-value",
            "client_secret=opaque-client-secret",
        ]
    )

    redacted = tui_observation.redact_observation_text(text)

    assert raw_openai_key not in redacted
    assert "sk-proj-" not in redacted
    assert "opaque-secret-value" not in redacted
    assert "opaque...alue" not in redacted
    assert "opaque-json-secret" not in redacted
    assert "opaque...cret" not in redacted
    assert raw_bearer not in redacted
    assert "bearer-token-" not in redacted
    assert "opaque-url-token" not in redacted
    assert "opaque-secret" not in redacted
    assert "opaque-lower-api-key" not in redacted
    assert "opaque-colon-token" not in redacted
    assert "opaque-password-value" not in redacted
    assert "opaque-client-secret" not in redacted


@pytest.mark.asyncio
async def test_list_tui_observation_sessions_surfaces_tmux_error(monkeypatch):
    async def fake_run_tmux(args, timeout=2.0):
        assert args[:3] == ["list-panes", "-a", "-F"]
        return 1, "", "tmux failed client_secret=opaque-list-secret"

    monkeypatch.setattr(tui_observation, "_run_tmux", fake_run_tmux)

    payload = await tui_observation.list_tui_observation_sessions()

    assert payload["object"] == "hermes.tui_observation.session.list"
    assert payload["schema"] == "tui.observation.v1"
    assert payload["sessions"] == []
    assert payload["status"] == "error"
    assert payload["reason"] == "tmux_list_panes_failed"
    assert payload["read_only"] is True
    assert payload["untrusted"] is True
    assert "opaque-list-secret" not in str(payload)


@pytest.mark.parametrize(
    ("screen", "expected_status", "expected_reason"),
    [
        ("Please log in to continue", "auth_required", "login_required_screen"),
        ("A new version of Claude Code is available", "update_prompt", "update_available_screen"),
        ("Do you want to proceed? (y/N)", "waiting_for_permission", "permission_prompt"),
        ("│ > Try asking something", "idle_ready", "input_prompt_visible"),
    ],
)
def test_classify_snapshot_status(screen, expected_status, expected_reason):
    status = tui_observation.classify_snapshot_status(screen, base_command="claude")

    assert status["status"] == expected_status
    assert status["reason"] == expected_reason
    assert status["confidence"] > 0
    assert status["evidence"]


@pytest.mark.asyncio
async def test_list_tui_observation_sessions_uses_real_contract_with_mocked_tmux(monkeypatch):
    async def fake_run_tmux(args, timeout=2.0):
        assert args[:3] == ["list-panes", "-a", "-F"]
        raw = "\t".join(
            [
                "work",
                "0",
                "claude",
                "1",
                "%12",
                "1",
                "claude",
                "Claude Code",
                "120",
                "40",
                "12345",
                "/repo",
            ]
        )
        return 0, raw, ""

    monkeypatch.setattr(tui_observation, "_run_tmux", fake_run_tmux)

    payload = await tui_observation.list_tui_observation_sessions()

    assert payload["object"] == "hermes.tui_observation.session.list"
    assert payload["schema"] == "tui.observation.v1"
    assert payload["read_only"] is True
    assert payload["sessions"][0]["pane_title"] == "Claude Code"
    assert payload["sessions"][0]["current_path"] == "/repo"
    assert payload["sessions"][0]["capture_command"] == "tmux capture-pane -p -t %12"


@pytest.mark.asyncio
async def test_capture_tui_observation_snapshot_uses_schema_and_forced_redaction(monkeypatch):
    async def fake_run_tmux(args, timeout=2.0):
        assert args[:4] == ["capture-pane", "-p", "-t", "%12"]
        return 0, "Please log in\nGITHUB_TOKEN=opaque-token-value\nAuthorization: Bearer opaque-access-token\n", ""

    monkeypatch.setattr(tui_observation, "_run_tmux", fake_run_tmux)

    payload = await tui_observation.capture_tui_observation_snapshot("%12", lines=40)

    assert payload["object"] == "hermes.tui_observation.snapshot"
    assert payload["schema"] == "tui.observation.v1"
    assert payload["status"] == "auth_required"
    assert "opaque-token-value" not in payload["terminal"]
    assert "opaque-access-token" not in payload["terminal"]
    assert payload["read_only"] is True
    assert payload["untrusted"] is True
