"""Regression test for the Codex GitHub PR review gate documentation.

The codex skill must spell out the GitHub `@codex review` review gate so that
implementations pushed to a GitHub branch/PR are not reported complete while the
`chatgpt-codex-connector[bot]` review is still pending. This guards the source
SKILL.md and the auto-generated Docusaurus page against silent drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SKILL_MD = REPO / "skills" / "autonomous-ai-agents" / "codex" / "SKILL.md"
GENERATED_DOC = (
    REPO
    / "website"
    / "docs"
    / "user-guide"
    / "skills"
    / "bundled"
    / "autonomous-ai-agents"
    / "autonomous-ai-agents-codex.md"
)

# Each required fragment maps an acceptance criterion to concrete wording.
REQUIRED_FRAGMENTS = (
    # `@codex review` posting step is documented.
    "@codex review",
    # Completion is confirmed by polling for the GitHub Codex bot.
    "chatgpt-codex-connector[bot]",
    # The gate is checked by polling GitHub.
    "poll",
    # A pending state alone must not count as a pass.
    "pending",
    # Failure/timeout must report blocked, never passed.
    "blocked",
    # Applies to coding subagents too, not just the parent agent.
    "subagent",
)


@pytest.mark.parametrize("doc", [SKILL_MD, GENERATED_DOC], ids=["skill_md", "generated_doc"])
def test_codex_pr_review_gate_documented(doc: Path) -> None:
    assert doc.exists(), f"missing doc: {doc}"
    text = doc.read_text(encoding="utf-8")
    lowered = text.lower()
    for fragment in REQUIRED_FRAGMENTS:
        assert fragment.lower() in lowered, f"{doc.name} is missing required wording: {fragment!r}"


def test_pending_is_not_a_pass_condition_stated() -> None:
    """The 'pending alone is never a pass' rule must be explicit, not implied."""
    text = SKILL_MD.read_text(encoding="utf-8").lower()
    assert "not a pass" in text or "never a pass" in text or "never as passed" in text
