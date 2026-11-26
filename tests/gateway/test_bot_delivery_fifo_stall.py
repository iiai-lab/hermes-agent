"""A Bot Chat FIFO never stalls behind a claim nobody executes, and never delivers a notice whose
named Kanban card is finished (archived or done).

A ``started`` head is normally a turn this owner is running. When the drain that claimed it dies
before the turn starts (a cancellation, a profile teardown racing the claim), the row stays
``started`` with no worker and no execution, and ``claim_session_input`` refuses every follower
behind it for good: one Bot Chat sat ~5 hours behind such a row. The owner now fences its own
orphaned claim at the next claim and the existing unattended policy settles it, so the followers run.

A Kanban status notice names its card. Once that card is archived or done the notice is stale: it
is dropped at the claim, which is also after it was queued, so a notice waiting in the FIFO for a
busy chat never spends a turn on a card nobody can act on.
"""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from gateway.session_authority import LiveSession, initialize_session_authority
from gateway.session_contract import SessionRef
from persistence.core.errors import AuthorityRefusal
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_session
from tests.waiting import async_wait_until

# Imported here rather than inside the drain: the drain imports it lazily on its first turn, and a
# worktree checkout that sits inside the real Hermes home refuses that read under the suite's
# home guard.
from gateway import session_managed_worker  # the drain imports it lazily; keep it loaded


async def _authority(tmp_path, monkeypatch, executed):
    """One real owner over SQLite, a telegram-native route ``s``, and a turn that records what ran."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'ACK'

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    runner = SimpleNamespace(_session_db=store.persistence, session_store=store, _draining=False,
                             _handle_message=None, _adapter_for_source=lambda source: None)
    seed_session(store.persistence, 's', 'telegram')
    authority = await initialize_session_authority(runner, profile_id='p', instance_id='owner-1')
    authority.sessions['s'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='bot-chat'), 's')
    return store, authority


def _notice(authority, text, request_id='notice-1', identity='proc-1'):
    row, _created = authority.persistence.admissions.admit(
        epoch=authority.epoch, principal_id='automation:agent:main:local:dm:bot-chat', session_id='s',
        request_id=request_id,
        payload={'text': text, 'local_automation_v1': {'identity': identity, 'owner': 's'}})
    return row


def _rows(authority):
    return [(r.request_id, r.status, r.outcome)
            for r in authority.persistence.admissions.list(session_id='s', pending_only=False)]


async def _run_drain(authority, real_schedule):
    authority._schedule = real_schedule
    authority._schedule(SessionRef('p', 's'))
    live = authority.sessions['s']
    await async_wait_until(lambda: live.task is not None and live.task.done(), timeout=60,
                           what='the drain to finish')


@pytest.mark.asyncio
async def test_the_claim_ledger_refuses_a_started_head_until_it_is_fenced(tmp_path, monkeypatch):
    """The durable contract behind the fix: a ``started`` head with nothing bound to it blocks every
    claim, and fencing it is what lets the next claim through."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        head = _notice(authority, 'HEAD', request_id='head', identity='proc-head')
        claimed = authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        assert claimed.generation == 1
        _notice(authority, 'FOLLOWER', request_id='follower', identity='proc-follower')
        assert authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s') is None

        fenced = authority.persistence.admissions.orphan_claim(epoch=authority.epoch, session_id='s',
                                                               running=claimed.generation)
        assert fenced is None, 'the generation this owner is running is never fenced'
        fenced = authority.persistence.admissions.orphan_claim(epoch=authority.epoch, session_id='s')
        assert fenced is not None and fenced.admission_id == head.admission_id
        assert fenced.status == 'unknown' and fenced.outcome is None
        # The fenced head is now the policy's to clear (an unattended row settles as interrupted,
        # a person's waits for Discard); once it is terminal the follower claims.
        with pytest.raises(AuthorityRefusal, match='unknown_execution'):
            authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        authority.persistence.admissions.resolve_unknown(epoch=authority.epoch, admission_id=head.admission_id,
                                                         generation=claimed.generation)
        next_claim = authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        assert next_claim is not None and next_claim.request_id == 'follower'
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_started_row_queued_behind_a_reverted_steer_is_still_fenced(tmp_path, monkeypatch):
    """The orphan need not be the head: a steer returned to its original ``seq`` sorts in front of it
    while the claim verb still refuses on the started row anywhere in the session."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        head = _notice(authority, 'HEAD', request_id='head', identity='proc-head')
        early = _notice(authority, 'EARLY', request_id='early', identity='proc-early')
        claimed = authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        assert claimed.admission_id == head.admission_id
        # A queued row that sorts BEFORE the claim's row, so the session has queued + started.
        raw = raw_holder(authority.persistence)
        raw._execute_write(lambda conn: conn.execute(
            "UPDATE session_admissions SET seq=0 WHERE admission_id=?", (early.admission_id,)))

        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        await _run_drain(authority, real_schedule)
        statuses = {request_id: (status, outcome) for request_id, status, outcome in _rows(authority)}
        # The orphaned claim was fenced, not silently skipped: the queued row ran and the session
        # is no longer stuck behind a started row.
        assert statuses['head'][0] in ('unknown', 'terminal')
        assert statuses['early'] == ('terminal', 'completed')
        assert executed == ['EARLY']
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_started_head_nobody_executes_does_not_stall_the_queue(tmp_path, monkeypatch):
    """The stall: a claimed turn whose drain died leaves ``started`` behind. The next drain fences
    it (the unattended policy then settles it) and runs the messages queued behind it."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        head = _notice(authority, 'HEAD', request_id='head', identity='proc-head')
        claimed = authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        assert claimed.admission_id == head.admission_id and claimed.generation == 1
        follower = _notice(authority, 'FOLLOWER', request_id='follower', identity='proc-follower')

        await _run_drain(authority, real_schedule)
        await async_wait_until(lambda: [r for r in _rows(authority) if r[0] == 'follower' and r[1] == 'terminal'],
                               timeout=30, what='the follower to settle')
        assert executed == ['FOLLOWER']
        statuses = {request_id: (status, outcome) for request_id, status, outcome in _rows(authority)}
        assert statuses['head'] == ('terminal', 'interrupted')  # fenced, never replayed
        assert statuses['follower'] == ('terminal', 'completed')
        assert follower.admission_id != head.admission_id
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_claim_this_owner_is_running_is_never_fenced(tmp_path, monkeypatch):
    """The fence names only generations this owner is NOT executing: a live claim stays started."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        _notice(authority, 'HEAD', request_id='head', identity='proc-head')
        claimed = authority.persistence.admissions.claim(epoch=authority.epoch, session_id='s')
        # The owner's own execution stamp for that generation (what ``_stamp_claim`` leaves behind).
        authority.executing['s'] = claimed.generation
        _notice(authority, 'FOLLOWER', request_id='follower', identity='proc-follower')

        await _run_drain(authority, real_schedule)
        statuses = {request_id: (status, outcome) for request_id, status, outcome in _rows(authority)}
        assert statuses['head'] == ('started', None)
        assert statuses['follower'] == ('queued', None)
        assert executed == []
    finally:
        store.close_all_db_handles()


def _kanban_board(tmp_path, monkeypatch):
    """A temp kanban home + pinned board DB; returns ``(kanban_db, kanban_db_connect)``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'kanban-home'))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    return kb, kbc


def _card(kb, kbc, title):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee='worker')


def _finish_card(kb, kbc, task_id, status):
    with kbc.connect_closing() as conn:
        if status == 'done':
            assert kb.complete_task(conn, task_id, summary='finished')
        elif status == 'archived':
            assert kb.archive_task(conn, task_id)
        else:
            raise AssertionError(f'unsupported finish status {status!r}')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['archived', 'done'])
async def test_a_notice_whose_card_finishes_while_it_waits_is_dropped(tmp_path, monkeypatch, status):
    """Queued while the card was live, delivered after it finished: the FIFO drops it, no turn runs."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        kb, kbc = _kanban_board(tmp_path, monkeypatch)
        task_id = _card(kb, kbc, 'card')
        text = f'[kanban] Task {task_id} is running.\nTitle: card\nBoard: default'
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        notice = _notice(authority, text, identity=f'kanban:{task_id}')
        assert _rows(authority) == [('notice-1', 'queued', None)]  # it IS in the FIFO
        _finish_card(kb, kbc, task_id, status)

        await _run_drain(authority, real_schedule)
        await async_wait_until(lambda: _rows(authority)[0][1] == 'terminal', timeout=30,
                               what='the notice to leave the FIFO')
        assert executed == []  # no turn, so nothing reached the chat
        assert _rows(authority) == [(notice.request_id, 'terminal', 'completed')]
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_notice_naming_a_live_card_is_still_delivered(tmp_path, monkeypatch):
    """Only a finished card drops the notice: a card still in flight keeps it."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        kb, kbc = _kanban_board(tmp_path, monkeypatch)
        task_id = _card(kb, kbc, 'card')
        text = f'[kanban] Task {task_id} is running.\nTitle: card\nBoard: default'
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        _notice(authority, text, identity=f'kanban:{task_id}')

        await _run_drain(authority, real_schedule)
        assert executed == [text]
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_notice_for_an_unknown_card_is_still_delivered(tmp_path, monkeypatch):
    """A board that does not hold the named card proves nothing: the notice is delivered."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        _kanban_board(tmp_path, monkeypatch)
        text = '[kanban] Task t_0000dead is running.\nTitle: elsewhere\nBoard: other'
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        _notice(authority, text, identity='kanban:t_0000dead')

        await _run_drain(authority, real_schedule)
        assert executed == [text]
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_kanban_card_attempt_is_never_dropped_by_the_notice_rule(tmp_path, monkeypatch):
    """The card's own work turn always runs, whatever its card's status says."""
    executed = []
    store, authority = await _authority(tmp_path, monkeypatch, executed)
    try:
        kb, kbc = _kanban_board(tmp_path, monkeypatch)
        task_id = _card(kb, kbc, 'card')
        _finish_card(kb, kbc, task_id, 'archived')
        text = f'work kanban task {task_id}'
        real_schedule, held = authority._schedule, []
        authority._schedule = lambda ref: held.append(ref)
        authority.persistence.admissions.admit(
            epoch=authority.epoch, principal_id='automation:agent:main:local:dm:bot-chat', session_id='s',
            request_id='card-attempt',
            payload={'text': text, 'finite': True,
                     'kanban_card_v1': {'board': 'default', 'task_id': task_id, 'run_id': 1}})

        await _run_drain(authority, real_schedule)
        assert executed == [text]
    finally:
        store.close_all_db_handles()
