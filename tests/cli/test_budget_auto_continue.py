import queue
from unittest.mock import MagicMock

from cli import HermesCLI


class _FakeSessionDB:
    def __init__(self, fail_create=False):
        self.fail_create = fail_create
        self.ended = []
        self.created = []
        self.system_prompts = []
        self.messages = []

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def create_session(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("create failed")
        self.created.append(kwargs)

    def append_message(self, **kwargs):
        self.messages.append(kwargs)

    def update_system_prompt(self, session_id, system_prompt):
        self.system_prompts.append((session_id, system_prompt))


def _cli_for_budget_continue(enabled=True, max_continuations=3):
    cli = HermesCLI.__new__(HermesCLI)
    cli.auto_continue_on_budget = enabled
    cli.auto_continue_on_budget_max_handoffs = max_continuations
    cli._budget_auto_continue_count = 0
    cli._pending_input = queue.Queue()
    cli._session_db = _FakeSessionDB()
    cli.session_id = "parent-session"
    cli.model = "test/model"
    cli.session_log_file = None
    cli.logs_dir = None
    cli._pending_title = "old-title"
    cli.conversation_history = [
        {"role": "user", "content": "do a long task"},
        {"role": "assistant", "content": "partial summary"},
    ]
    cli.agent = MagicMock()
    cli.agent.max_iterations = 120
    cli.agent.session_id = "parent-session"
    cli.agent.session_log_file = None
    cli.agent._cached_system_prompt = "system prompt"
    cli.agent._last_flushed_db_idx = 99
    return cli


def test_budget_auto_continue_disabled_does_not_queue_or_split_session():
    cli = _cli_for_budget_continue(enabled=False)
    result = {"completed": False, "interrupted": False, "api_calls": 120}

    assert cli._handle_budget_auto_continue(result) is False

    assert cli._pending_input.empty()
    assert cli.session_id == "parent-session"
    assert cli._session_db.ended == []
    assert cli._session_db.created == []


def test_budget_auto_continue_splits_to_child_session_and_queues_continuation():
    cli = _cli_for_budget_continue(enabled=True)
    result = {"completed": False, "interrupted": False, "api_calls": 120}

    assert cli._handle_budget_auto_continue(result) is True

    assert cli._session_db.ended == [("parent-session", "iteration_budget_handoff")]
    assert cli._session_db.created
    created = cli._session_db.created[0]
    assert created["parent_session_id"] == "parent-session"
    assert created["model"] == "test/model"
    assert cli.session_id != "parent-session"
    assert cli.agent.session_id == cli.session_id
    assert cli.agent._last_flushed_db_idx == len(cli.conversation_history)
    assert cli._pending_title is None

    queued = cli._pending_input.get_nowait()
    assert "Continue the previous task" in queued
    assert "Do not repeat the summary" in queued


def test_budget_auto_continue_ignores_completed_and_interrupted_results():
    cli = _cli_for_budget_continue(enabled=True)

    assert cli._handle_budget_auto_continue({"completed": True, "api_calls": 120}) is False
    assert cli._handle_budget_auto_continue({"completed": False, "interrupted": True, "api_calls": 120}) is False
    assert cli._pending_input.empty()
    assert cli.session_id == "parent-session"


def test_budget_auto_continue_does_not_queue_when_pending_input_already_exists():
    cli = _cli_for_budget_continue(enabled=True)
    cli._pending_input.put("/new")

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120}) is False

    assert cli.session_id == "parent-session"
    assert cli._session_db.ended == []
    assert cli._pending_input.get_nowait() == "/new"


def test_budget_auto_continue_does_not_queue_when_handoff_session_fails():
    cli = _cli_for_budget_continue(enabled=True)
    cli._session_db = _FakeSessionDB(fail_create=True)

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120}) is False

    assert cli.session_id == "parent-session"
    assert cli._pending_input.empty()
    assert cli._session_db.ended == []


def test_budget_auto_continue_copies_history_to_child_session():
    cli = _cli_for_budget_continue(enabled=True)

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120}) is True

    child_session_id = cli._session_db.created[0]["session_id"]
    assert [m["session_id"] for m in cli._session_db.messages] == [child_session_id, child_session_id]
    assert [m["role"] for m in cli._session_db.messages] == ["user", "assistant"]
    assert cli.agent._last_flushed_db_idx == len(cli.conversation_history)


def test_budget_auto_continue_stops_after_configured_handoff_limit():
    cli = _cli_for_budget_continue(enabled=True, max_continuations=1)

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120}) is True
    cli._pending_input.get_nowait()
    cli.session_id = "second-parent"
    cli.agent.session_id = "second-parent"

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120}) is False

    assert cli._pending_input.empty()
    assert cli._budget_auto_continue_count == 1


def test_budget_auto_continue_ignores_result_with_leftover_steer():
    cli = _cli_for_budget_continue(enabled=True)

    assert cli._handle_budget_auto_continue({"completed": False, "api_calls": 120, "pending_steer": "user correction"}) is False

    assert cli.session_id == "parent-session"
    assert cli._session_db.ended == []
    assert cli._pending_input.empty()


def test_budget_auto_continue_count_resets_after_completed_turn():
    cli = _cli_for_budget_continue(enabled=True)
    cli._budget_auto_continue_count = 3

    cli._reset_budget_auto_continue_chain({"completed": True})

    assert cli._budget_auto_continue_count == 0
