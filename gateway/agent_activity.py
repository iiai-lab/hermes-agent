"""Stable user-visible agent activity event contract for Gateway clients."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

AGENT_ACTIVITY_SCHEMA = "agent.activity.v1"

ACTIVITY_STREAMS = frozenset(
    {
        "thinking",
        "tool",
        "delegate",
        "lifecycle",
        "compaction",
    }
)

ACTIVITY_PHASES = frozenset(
    {
        "start",
        "update",
        "completed",
        "failed",
        "cancelled",
    }
)

_PHASE_ALIASES = {
    "started": "start",
    "call": "start",
    "result": "completed",
    "end": "completed",
    "finished": "completed",
    "error": "failed",
}


def normalize_activity_phase(value: object) -> Optional[str]:
    phase = str(value or "").strip().lower()
    if phase in ACTIVITY_PHASES:
        return phase
    return _PHASE_ALIASES.get(phase)


def compact_display_text(value: object, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    if limit <= 3:
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _timestamp_to_iso(timestamp: object) -> str:
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc).isoformat()
        return timestamp.astimezone(timezone.utc).isoformat()
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    if isinstance(timestamp, str) and timestamp.strip():
        return timestamp.strip()
    return datetime.now(timezone.utc).isoformat()


def build_agent_activity_event(
    *,
    session_key: str,
    stream: str,
    phase: str,
    run_id: Optional[str] = None,
    timestamp: object = None,
    data: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_stream = str(stream or "").strip().lower()
    if normalized_stream not in ACTIVITY_STREAMS:
        raise ValueError(f"Unsupported activity stream: {stream!r}")

    normalized_phase = normalize_activity_phase(phase)
    if normalized_phase is None:
        raise ValueError(f"Unsupported activity phase: {phase!r}")

    payload: Dict[str, Any] = {
        "schema": AGENT_ACTIVITY_SCHEMA,
        "sessionKey": session_key,
        "stream": normalized_stream,
        "phase": normalized_phase,
        "timestamp": _timestamp_to_iso(timestamp),
        "data": dict(data or {}),
    }
    if run_id:
        payload["runId"] = run_id
    return payload
