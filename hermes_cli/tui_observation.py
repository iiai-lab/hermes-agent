"""Read-only tmux TUI observation helpers for Hermes dashboard/API surfaces.

This module intentionally exposes observation-only primitives.  It never sends
input to tmux panes and treats captured terminal text as untrusted, redacted
screen content.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any, Dict, List, Tuple

from agent.redact import redact_sensitive_text

MAX_CAPTURE_LINES = 200
MAX_CAPTURE_BYTES = 65_536
TMUX_LIST_FORMAT = "\t".join(
    [
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_active}",
        "#{pane_current_command}",
        "#{pane_title}",
        "#{pane_width}",
        "#{pane_height}",
        "#{pane_pid}",
        "#{pane_current_path}",
    ]
)

_PANE_ID_RE = re.compile(r"^%\d+$")
_TUI_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|token|secret|password)=)[^&\s]+",
    re.IGNORECASE,
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b[\w.-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|token|secret|password)[\w.-]*\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"),
    re.compile(r"(?i)([\"']?[\w.-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|token|secret|password)[\w.-]*[\"']?\s*:\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"),
    re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[^\s]+"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def validate_pane_id(pane_id: str) -> str:
    """Return a safe tmux pane id or raise ValueError.

    Only tmux pane IDs like ``%12`` are accepted.  Arbitrary tmux targets are
    deliberately rejected to avoid target-string injection and accidental
    session/window operations.
    """
    pane_id = str(pane_id or "").strip()
    if not _PANE_ID_RE.fullmatch(pane_id):
        raise ValueError("invalid_pane_id")
    return pane_id


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _detect_agent_kind(command: str, title: str, window_name: str) -> str:
    haystack = " ".join([command or "", title or "", window_name or ""]).lower()
    if "claude" in haystack:
        return "claude-code"
    if "codex" in haystack:
        return "codex"
    if "hermes" in haystack:
        return "hermes"
    return "terminal"


def _redact_observation_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_observation_text(value)
    return value


def _session_evidence(command: str, title: str) -> List[str]:
    evidence: List[str] = []
    if command:
        evidence.append(redact_observation_text(f"command={command}"))
    if title:
        evidence.append(redact_observation_text(f"title={title}"))
    return evidence or ["tmux pane listed"]


def parse_tmux_panes(output: str) -> List[Dict[str, Any]]:
    """Parse ``tmux list-panes`` tab-separated output into session records."""
    sessions: List[Dict[str, Any]] = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        (
            session_name,
            window_index,
            window_name,
            pane_index,
            pane_id,
            pane_active,
            command,
            title,
            width,
            height,
            pid,
            cwd,
        ) = parts[:12]
        try:
            safe_pane_id = validate_pane_id(pane_id)
        except ValueError:
            continue
        agent_kind = _detect_agent_kind(command, title, window_name)
        known_agent = agent_kind in {"claude-code", "codex", "hermes"}
        evidence = _session_evidence(command, title)
        safe_session_name = redact_observation_text(session_name)
        safe_window_name = redact_observation_text(window_name)
        safe_title = redact_observation_text(title)
        safe_command = redact_observation_text(command)
        safe_cwd = redact_observation_text(cwd)
        sessions.append(
            {
                "id": safe_pane_id,
                "pane_id": safe_pane_id,
                "session_name": safe_session_name,
                "window_index": _to_int(window_index),
                "window_name": safe_window_name,
                "pane_index": _to_int(pane_index),
                "active": pane_active == "1",
                "command": safe_command,
                "title": safe_title,
                "pane_title": safe_title,
                "width": _to_int(width),
                "height": _to_int(height),
                "pid": _to_int(pid),
                "cwd": safe_cwd,
                "current_path": safe_cwd,
                "dead": False,
                "agent_kind": agent_kind,
                "label": f"{safe_session_name}:{window_index}.{safe_window_name}:{safe_pane_id}",
                "status": "possibly_idle" if known_agent else "unknown",
                "reason": "known_agent_detected" if known_agent else "tmux_pane_listed",
                "confidence": 0.35 if known_agent else 0.25,
                "evidence": evidence,
                "attach_command": f"tmux attach-session -t {shlex.quote(safe_session_name)}",
                "capture_command": f"tmux capture-pane -p -t {safe_pane_id}",
            }
        )
    return sessions


def redact_observation_text(text: str) -> str:
    """Redact terminal-observed text at the API safety boundary."""
    redacted = text or ""
    redacted = _TUI_QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}***", redacted)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = redact_sensitive_text(redacted, force=True)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_observation_lines(lines: List[str]) -> List[str]:
    return redact_observation_text("\n".join(lines)).splitlines()


def classify_snapshot_status(screen: str, *, base_command: str = "") -> Dict[str, Any]:
    """Classify a captured TUI screen using conservative heuristics."""
    lower = (screen or "").lower()
    command = (base_command or "").lower()

    checks = [
        (
            ("log in" in lower or "login" in lower or "authentication" in lower or "auth required" in lower),
            "auth_required",
            "login_required_screen",
            0.91,
            "visible: login/authentication prompt",
        ),
        (
            ("update available" in lower or "new version" in lower or "upgrade" in lower),
            "update_prompt",
            "update_available_screen",
            0.86,
            "visible: update prompt",
        ),
        (
            ("do you want to proceed" in lower or "approve" in lower or "permission" in lower or "allow" in lower),
            "waiting_for_permission",
            "permission_prompt",
            0.82,
            "visible: permission prompt",
        ),
        (
            ("try asking" in lower or "│ >" in screen or "\n> " in screen or lower.rstrip().endswith(">")),
            "idle_ready",
            "input_prompt_visible",
            0.7,
            "visible: input prompt",
        ),
    ]
    for matched, status, reason, confidence, evidence in checks:
        if matched:
            return {
                "status": status,
                "reason": reason,
                "confidence": confidence,
                "evidence": [evidence],
            }

    if command in {"claude", "codex", "hermes"}:
        return {
            "status": "possibly_idle",
            "reason": "known_agent_screen_without_activity_marker",
            "confidence": 0.35,
            "evidence": [f"command={base_command}"],
        }
    return {
        "status": "unknown",
        "reason": "unclassified_screen",
        "confidence": 0.2,
        "evidence": ["tmux capture succeeded"],
    }


async def _run_tmux(args: List[str], *, timeout: float = 2.0) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "tmux command timed out"
    stdout = stdout_b[:MAX_CAPTURE_BYTES].decode(errors="replace")
    stderr = stderr_b[:MAX_CAPTURE_BYTES].decode(errors="replace")
    return proc.returncode or 0, stdout, stderr


async def get_tui_observation_status() -> Dict[str, Any]:
    code, stdout, stderr = await _run_tmux(["-V"], timeout=2.0)
    available = code == 0
    payload: Dict[str, Any] = {
        "object": "hermes.tui_observation.status",
        "schema": "tui.observation.v1",
        "available": available,
        "read_only": True,
        "redaction": {"enabled": True},
        "limits": {"max_lines": MAX_CAPTURE_LINES, "max_bytes": MAX_CAPTURE_BYTES},
    }
    if available:
        payload["tmux_version"] = stdout.strip()
    else:
        payload["error"] = "tmux_not_available"
        if stderr.strip():
            payload["detail"] = stderr.strip()[:200]
    return payload


async def list_tui_observation_sessions() -> Dict[str, Any]:
    code, stdout, stderr = await _run_tmux(["list-panes", "-a", "-F", TMUX_LIST_FORMAT], timeout=2.0)
    sessions: List[Dict[str, Any]] = []
    payload: Dict[str, Any] = {
        "object": "hermes.tui_observation.session.list",
        "schema": "tui.observation.v1",
        "sessions": sessions,
        "read_only": True,
        "untrusted": True,
        "redaction": {"enabled": True},
    }
    if code != 0:
        payload.update(
            {
                "status": "error",
                "reason": "tmux_list_panes_failed",
                "error": redact_observation_text((stderr or "tmux list-panes failed").strip()[:200]),
            }
        )
        return payload

    now = time.time()
    for session in parse_tmux_panes(stdout):
        item = dict(session)
        item["updated_at"] = now
        item["read_only"] = True
        item["untrusted"] = True
        sessions.append(item)
    payload["status"] = "ok"
    return payload


async def capture_tui_observation_snapshot(pane_id: str, *, lines: int = 80) -> Dict[str, Any]:
    safe_pane_id = validate_pane_id(pane_id)
    try:
        capped_lines = max(1, min(int(lines), MAX_CAPTURE_LINES))
    except (TypeError, ValueError):
        capped_lines = 80
    code, stdout, stderr = await _run_tmux(
        ["capture-pane", "-p", "-t", safe_pane_id, "-S", f"-{capped_lines}"],
        timeout=2.0,
    )
    captured_at = time.time()
    if code != 0:
        return {
            "object": "hermes.tui_observation.snapshot",
            "schema": "tui.observation.v1",
            "id": safe_pane_id,
            "pane_id": safe_pane_id,
            "status": "error",
            "reason": "tmux_capture_failed",
            "confidence": 0.0,
            "evidence": ["tmux capture-pane failed"],
            "terminal": "",
            "lines": [],
            "line_count": 0,
            "captured_at": captured_at,
            "error": redact_observation_text((stderr or "tmux capture failed").strip()[:200]),
            "read_only": True,
            "untrusted": True,
            "redaction": {"enabled": True},
        }

    redacted_terminal = redact_observation_text(stdout.rstrip("\n"))
    redacted_lines = redacted_terminal.splitlines()
    classification = classify_snapshot_status(redacted_terminal)
    return {
        "object": "hermes.tui_observation.snapshot",
        "schema": "tui.observation.v1",
        "id": safe_pane_id,
        "pane_id": safe_pane_id,
        **classification,
        "terminal": redacted_terminal,
        "lines": redacted_lines,
        "line_count": len(redacted_lines),
        "captured_at": captured_at,
        "read_only": True,
        "untrusted": True,
        "redaction": {"enabled": True},
    }
