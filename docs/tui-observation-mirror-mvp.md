# TUI Observation / Terminal Mirror MVP

Goal: show read-only Claude Code / Codex / other tmux panes in the Hermes dashboard without sending input.

## Scope

- Shared backend helper: enumerate tmux panes and capture a pane snapshot.
- Dashboard endpoints:
  - `GET /api/tui/sessions`
  - `GET /api/tui/sessions/{session_id}/snapshot?lines=80`
- API server endpoints:
  - `GET /v1/tui-observation/status`
  - `GET /v1/tui-observation/sessions`
  - `GET /v1/tui-observation/snapshot?pane_id=%251&lines=80`
- Web dashboard page: `/terminal-mirror`.

## Safety rules

- Read-only only. Do not add send-keys, kill, resize, or attach side effects.
- Treat screen content as untrusted and potentially secret-bearing.
- Always redact common secrets before returning screen text.
- Do not log captured terminal content.
- Accept only tmux pane IDs shaped like `%123`; reject arbitrary tmux targets.
- Report status with confidence/evidence; do not call idle/completion final without evidence.

## Contract

Session:

```json
{
  "id": "%12",
  "pane_id": "%12",
  "label": "claude:%12",
  "agent_kind": "claude-code",
  "status": "idle_ready_after_activity",
  "reason": "known_agent_command",
  "confidence": 0.55,
  "evidence": ["command=claude"],
  "attach_command": "tmux attach-session -t session",
  "updated_at": 1778123456.123
}
```

Snapshot:

```json
{
  "id": "%12",
  "pane_id": "%12",
  "status": "auth_required",
  "confidence": 0.91,
  "evidence": ["visible: login required"],
  "terminal": "...redacted screen text...",
  "lines": ["..."],
  "captured_at": 1778123456.123,
  "read_only": true,
  "untrusted": true,
  "redaction": {"enabled": true}
}
```

## Test plan

1. Unit-test pane ID validation, tmux list parsing, redaction, status classification.
2. Test dashboard endpoints with monkeypatched helper functions and session-token auth.
3. Test API server endpoints with aiohttp TestClient and API-key auth.
4. Build/typecheck web dashboard after adding the page.
