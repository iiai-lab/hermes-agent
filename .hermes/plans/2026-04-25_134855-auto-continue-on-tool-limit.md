# Hermes Agent Auto-Continue on Tool/Iteration Limit Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** When Hermes reaches its per-turn tool-calling/iteration limit, it should automatically create a resumable handoff and continue in a fresh session/turn instead of stopping with only a summary.

**Architecture:** Reuse the existing context-compression session split pattern in `run_agent.py` rather than inventing a parallel session system. Add a structured continuation signal to `AIAgent.run_conversation()` results, then let CLI/gateway callers perform bounded auto-continue loops. The agent core should persist a compact handoff transcript, rotate the SQLite session with `parent_session_id`, reset iteration budget, and seed the next turn with a continuation prompt.

**Tech Stack:** Python, Hermes Agent core (`run_agent.py`), CLI orchestration (`cli.py`), gateway orchestration (`gateway/run.py`), config defaults (`hermes_cli/config.py`), pytest.

---

## Current Context

Existing behavior found in `/home/slowlife/.hermes/hermes-agent`:

- `run_agent.py:9249` loops while `api_call_count < self.max_iterations` and `iteration_budget.remaining > 0`.
- `run_agent.py:12039-12056` currently handles exhaustion by asking for a toolless summary via `_handle_max_iterations()`.
- `run_agent.py:7776-7881` already has `_compress_context()`, which:
  - flushes memory,
  - compresses messages,
  - ends the old SQLite session with `end_reason="compression"`,
  - creates a child session with `parent_session_id=old_session_id`,
  - resets `_last_flushed_db_idx`,
  - updates token estimates.
- Existing config has `compression.auto_handoff_after` documented in the skill, but current checked source `hermes_cli/config.py:516-522` only shows basic compression keys in the default config in this checkout.
- Working tree is already dirty before this plan:
  - `agent/context_compressor.py`
  - `cli.py`
  - `run_agent.py`
  - `tools/checkpoint_manager.py`
  - `ui-tui/package-lock.json`
  - `web/package-lock.json`

Because the tree is dirty, implementation should be done in an isolated git worktree or after preserving existing changes.

---

## Recommended Route

Implement as a configurable, bounded auto-continuation feature:

```yaml
agent:
  max_turns: 90
  auto_continue_on_max_iterations: true
  auto_continue_max_handoffs: 3
  auto_continue_summary_budget_tokens: 4000
```

Behavior:

1. On max-iteration/tool-budget exhaustion, do not just final-answer and stop.
2. Generate a handoff summary with explicit sections:
   - original user goal
   - completed actions
   - current state
   - pending next actions
   - blockers/errors
   - files/commands touched
   - verification still needed
3. Persist the current session and end it with `end_reason="max_iterations_handoff"`.
4. Create a child session with `parent_session_id` pointing to the exhausted session.
5. Reset turn-scoped counters/iteration budget for the continuation.
6. Return structured metadata from `run_conversation()` such as:

```python
{
    "final_response": "...handoff notice or final summary...",
    "messages": messages,
    "auto_continue": {
        "reason": "max_iterations",
        "handoff_prompt": "...",
        "old_session_id": old_session_id,
        "new_session_id": self.session_id,
        "handoff_count": n,
    },
}
```

7. CLI/gateway callers detect `auto_continue` and immediately invoke `run_conversation()` again with the handoff prompt, capped by `auto_continue_max_handoffs`.
8. If the continuation also exhausts, repeat up to the configured limit, then stop with a clear handoff artifact for manual resume.

---

## Files Likely to Change

- Modify: `/home/slowlife/.hermes/hermes-agent/run_agent.py`
  - Add max-iteration handoff creation helper.
  - Add session rotation helper shared with `_compress_context()` if practical.
  - Return structured `auto_continue` metadata.
- Modify: `/home/slowlife/.hermes/hermes-agent/hermes_cli/config.py`
  - Add default config keys and display support if needed.
- Modify: `/home/slowlife/.hermes/hermes-agent/cli.py`
  - Wrap normal interactive and `-q` calls with bounded auto-continue loop.
- Modify: `/home/slowlife/.hermes/hermes-agent/gateway/run.py`
  - Add bounded auto-continue loop for gateway messages without spamming users.
- Modify/Create tests under:
  - `/home/slowlife/.hermes/hermes-agent/tests/run_agent/`
  - `/home/slowlife/.hermes/hermes-agent/tests/cli/` if existing structure supports it
  - `/home/slowlife/.hermes/hermes-agent/tests/gateway/` if existing structure supports it

---

## Step-by-Step Plan

### Task 1: Preserve current dirty work and create an isolated implementation branch

**Objective:** Avoid overwriting existing local edits.

**Files:** none initially.

**Steps:**

1. Inspect current status:
   ```bash
   git status --short
   git diff --stat
   ```
2. Create an isolated worktree from `main`:
   ```bash
   git worktree add ../hermes-agent-auto-continue -b feat/auto-continue-on-max-iterations main
   ```
3. Work in:
   ```bash
   /home/slowlife/.hermes/hermes-agent-auto-continue
   ```

**Verification:** `git status --short` in the worktree should be clean.

---

### Task 2: Add config defaults

**Objective:** Make the feature opt-in or default-on with bounded safety.

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `cli-config.yaml.example`

**Implementation sketch:**

Add to `DEFAULT_CONFIG["agent"]`:

```python
"auto_continue_on_max_iterations": True,
"auto_continue_max_handoffs": 3,
"auto_continue_summary_budget_tokens": 4000,
```

**Tests:** Existing config tests, if present.

**Verification:**

```bash
python -m pytest tests -k config -q -o 'addopts='
```

---

### Task 3: Add handoff helper in `AIAgent`

**Objective:** Convert exhaustion into a structured handoff instead of plain stop.

**Files:**
- Modify: `run_agent.py`
- Test: `tests/run_agent/test_auto_continue_handoff.py`

**Implementation sketch:**

Add helpers:

```python
def _should_auto_continue_on_max_iterations(self) -> bool:
    return bool(self._agent_auto_continue_on_max_iterations)


def _build_max_iteration_handoff_prompt(self, handoff_summary: str) -> str:
    return (
        "Continue the previous Hermes Agent task from this handoff. "
        "Do not ask the user to repeat context. Use tools as needed and keep working until complete.\n\n"
        + handoff_summary
    )
```

Add a handoff summary request that is toolless and specifically asks for continuation state, not user-facing final prose.

**Test cases:**

1. When `auto_continue_on_max_iterations=False`, old behavior remains.
2. When enabled and budget exhausts, result includes `auto_continue.reason == "max_iterations"`.
3. The handoff prompt includes pending next actions and previous session ID.
4. No tool schemas are passed to the handoff summary call.

**Verification:**

```bash
venv/bin/python -m pytest tests/run_agent/test_auto_continue_handoff.py -q -o 'addopts='
```

---

### Task 4: Rotate session on max-iteration handoff

**Objective:** Start the continuation with fresh session-scoped runtime while preserving lineage.

**Files:**
- Modify: `run_agent.py`
- Test: `tests/run_agent/test_auto_continue_handoff.py`

**Implementation sketch:**

Refactor the session split logic from `_compress_context()` into a helper such as:

```python
def _rotate_session(self, end_reason: str, *, new_system_prompt: str | None = None) -> tuple[str, str]:
    old_session_id = self.session_id
    self._session_db.end_session(old_session_id, end_reason)
    self.session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    self.session_log_file = self.logs_dir / f"session_{self.session_id}.json"
    self._session_db.create_session(
        session_id=self.session_id,
        source=self.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
        model=self.model,
        parent_session_id=old_session_id,
    )
    if new_system_prompt:
        self._session_db.update_system_prompt(self.session_id, new_system_prompt)
    self._last_flushed_db_idx = 0
    return old_session_id, self.session_id
```

Use end reason:

```python
"max_iterations_handoff"
```

**Test cases:**

- Old session is ended with `max_iterations_handoff`.
- New session has `parent_session_id == old_session_id`.
- Session title propagation still works if implemented.

---

### Task 5: Reset runtime budget for continuation

**Objective:** New continuation gets a fresh iteration budget without losing compressed/handoff context.

**Files:**
- Modify: `run_agent.py`

**Implementation sketch:**

Add a method:

```python
def _reset_iteration_runtime_for_handoff(self) -> None:
    self.iteration_budget = IterationBudget(self.max_iterations)
    self._budget_exhausted_injected = False
    self._budget_grace_call = False
    self._api_call_count = 0
```

Call it only after the exhausted turn is persisted/rotated and before returning `auto_continue` metadata.

**Verification:** Unit test asserts `iteration_budget.used == 0` after handoff setup.

---

### Task 6: CLI auto-continue loop

**Objective:** Make interactive CLI and one-shot `hermes chat -q` continue automatically.

**Files:**
- Modify: `cli.py`

**Implementation sketch:**

Wrap call sites around `self.agent.run_conversation(...)`:

```python
handoffs = 0
while True:
    result = self.agent.run_conversation(...)
    meta = result.get("auto_continue") or {}
    if not meta:
        break
    handoffs += 1
    if handoffs > self._auto_continue_max_handoffs:
        break
    agent_message = meta["handoff_prompt"]
    conversation_history = result.get("messages", [])
```

Important constraints:

- Do not print every internal handoff as a full user-facing message.
- Show concise status: `↪ Auto-continuing after iteration limit (1/3)...`
- Do not recurse; use a loop.
- Do not continue after user interrupt.

**Tests:** mock `run_conversation()` returning `auto_continue` once then final result.

---

### Task 7: Gateway auto-continue loop

**Objective:** Messaging platforms should continue without leaking noisy automation.

**Files:**
- Modify: `gateway/run.py`

**Implementation sketch:**

At the main message handling call around `agent.run_conversation(...)`, loop similarly to CLI, but emit only minimal platform status if supported.

Rules:

- Never create duplicate user-visible final messages.
- Stop on platform/user interrupt.
- Cap by config.
- Preserve `session_key` and update session entry if the agent rotates `session_id`.

**Tests:** gateway unit test if available; otherwise focused regression around result handling.

---

### Task 8: Verification and review

**Objective:** Prove the feature works and does not regress existing behavior.

**Commands:**

```bash
venv/bin/python -m pytest tests/run_agent/test_auto_continue_handoff.py -q -o 'addopts='
venv/bin/python -m pytest tests/test_hermes_state.py -q -o 'addopts='
venv/bin/python -m pytest tests/run_agent -q -o 'addopts='
```

Manual smoke test with tiny budget:

```bash
HERMES_HOME=/tmp/hermes-auto-continue-test hermes chat -q "Use tools to inspect this repo and keep working until you summarize findings" --toolsets terminal,file
```

Temporarily configure:

```yaml
agent:
  max_turns: 2
  auto_continue_on_max_iterations: true
  auto_continue_max_handoffs: 2
```

Expected:

- Hermes reaches iteration limit.
- It emits concise auto-continue status.
- It creates a child session.
- It continues with fresh budget.
- It stops after completion or after max handoffs with a useful handoff summary.

Final review:

```bash
git diff --stat
codex --approval-mode never --sandbox workspace-write "Review this Hermes Agent diff for correctness, regressions, and missing tests. Focus on auto-continue on max iterations."
```

Address Codex feedback before considering the work complete.

---

## Risks and Tradeoffs

- **Infinite continuation loops:** mitigate with `auto_continue_max_handoffs`.
- **Noisy gateway messages:** keep handoff messages internal; emit only concise status.
- **Session lineage confusion:** use the existing `parent_session_id` pattern and a distinct `end_reason="max_iterations_handoff"`.
- **Context bloat:** use compact handoff prompt rather than replaying full history; optionally run `_compress_context()` if token pressure is high.
- **Tool-call provider constraints:** handoff summary call must be toolless to avoid immediately consuming more tool budget.
- **Dirty working tree:** use worktree/branch to avoid mixing unrelated fixes.

---

## Open Questions

1. Should the feature be default-on? Recommended: default-on with cap `3`, because it matches the user's desired closed-loop behavior.
2. Should the handoff create a new SQLite session always, or only when continuing beyond the limit? Recommended: always rotate for clear lineage and fresh runtime.
3. Should this apply to subagents/delegate tasks too? Recommended: no for first implementation; delegate tasks already have independent `delegation.max_iterations` and should return bounded summaries to parent.
