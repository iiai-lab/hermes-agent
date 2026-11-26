"""Dispatcher-spawned kanban workers are supervised children.

The client imports ``hermes_bootstrap``, so ``prepare_launch`` runs with an
empty argv. ``HERMES_SUPERVISED_CHILD=1`` leaves a current install's
source-update tail to ``hermes update``. The spawn also keeps the worker's
stdin sealed, and does not write the marker into the parent's environment.
"""
import os
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def test_worker_spawn_marks_supervised_child_and_seals_stdin(monkeypatch, tmp_path):
    """The child env carries the supervisor marker; the parent env does not."""
    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        captured["stdin"] = kwargs["stdin"]
        return _Proc()

    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")

    task = kb.Task(
        id="t_b21733fb",
        title="ship it",
        body=None,
        assignee="default",
        status="in_progress",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace, exist_ok=True)

    kbd._default_spawn(task, workspace)

    assert captured["env"] is not os.environ
    assert captured["env"]["HERMES_SUPERVISED_CHILD"] == "1"
    assert captured["stdin"] is subprocess.DEVNULL
    assert os.environ.get("HERMES_SUPERVISED_CHILD") is None
