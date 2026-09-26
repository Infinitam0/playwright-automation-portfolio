"""Draft lint -- the automated guardrail between an LLM draft and the export.
Returns a list of violations (empty == clean)."""

from __future__ import annotations

import re

EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff←-⇿]"
)

# No unfilled mustache token may reach a draft: a stray {{your name}} from an
# unconfigured sender identity is unsendable, so the guard is on the token
# shape, not one name.
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")

# Every mail carries an opt-out. sender_block() adds it; a draft that lost it is
# unsendable.
_REQUIRED = (("unsubscribe", "an opt-out line"),)


def lint_draft(subject: str, body: str) -> list[str]:
    text = f"{subject}\n{body}"
    violations: list[str] = []

    if EMOJI_RE.search(text):
        violations.append("contains emoji")
    for token in sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(text)}):
        violations.append(f"contains unfilled placeholder {token}")
    for needle, what in _REQUIRED:
        if needle.lower() not in body.lower():
            violations.append(f"missing {what} ({needle!r})")

    return violations
