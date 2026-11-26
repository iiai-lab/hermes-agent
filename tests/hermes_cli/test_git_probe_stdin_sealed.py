"""Git probes must not inherit a kanban worker's stdin.

A worker's control reader sits on the inherited stdin. On Windows, a git
child that reads that handle again waits on UCRT's per-fd lock. Every git
probe these paths spawn therefore passes ``stdin=subprocess.DEVNULL``.
"""
from __future__ import annotations

import subprocess

from hermes_cli.kanban_db import _git_out
from hermes_cli.version_info import _git_version_info, _run_git


def test_git_probes_seal_stdin(tmp_path, monkeypatch):
    """The three git probe sites hand the child a sealed stdin."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run", fake_run)
    monkeypatch.setattr("hermes_cli.version_info.subprocess.run", fake_run)
    repo = tmp_path / "repo"
    repo.mkdir()

    _git_out(repo, "rev-parse", "HEAD")
    _run_git(repo, "rev-parse", "HEAD")
    marked = len(calls)
    _git_version_info(repo)

    assert len(calls) > marked
    assert calls[0][1].get("stdin") is subprocess.DEVNULL
    assert calls[1][1].get("stdin") is subprocess.DEVNULL
    status = [item for item in calls[marked:] if "status" in item[0]]
    assert status, "the dirty probe did not run"
    assert all(item[1].get("stdin") is subprocess.DEVNULL for item in status)
