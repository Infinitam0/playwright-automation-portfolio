"""Regex patterns for replacement-seeking signal extraction.

Each entry is (signal_type, compiled_pattern, base_confidence). The named group
`app` captures the candidate app name. High-confidence patterns (>=0.80) carry
shutdown vocabulary that is itself the strong signal; lower-confidence patterns
require catalog-gating in the extractor to avoid noise like "alternative to it".

Tweak base confidences after running scanner validate against the validation
set — the scoring formula sums their contributions.
"""

from __future__ import annotations

import re

# Captures a candidate app: either a quoted string of 2-40 chars, or one to
# three TitleCased word tokens. We rely on (?-i:...) to keep the leading-cap
# constraint even when the surrounding pattern is case-insensitive.
APP = (
    r'(?P<app>"[^"]{2,40}"|'
    r"(?-i:[A-Z][\w.+-]{1,30}(?:\s[A-Z][\w.+-]{1,30}){0,2}))"
)

# Format: (signal_type, pattern, base_confidence)
# Lower-confidence (alt_to / replace / instead / moving) demands a catalog hit.
# Higher-confidence (shutdown / dead / disc / rip / eol) is itself the signal.
# A small filler-word allowance handles natural phrasings like
# "Mailbox is also dead" or "Wunderlist is finally shutting down". Up to 2
# intervening words; we keep adjacency tight enough that the noun stays
# anchored to the verb phrase.
FILLER = r"(?:\w+\s+){0,2}"

PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("alt_to", re.compile(rf"(?i:\balternatives?\s+to\s+){APP}"), 0.45),
    ("replace", re.compile(rf"(?i:\breplacement\s+for\s+){APP}"), 0.55),
    ("instead", re.compile(rf"(?i:\binstead\s+of\s+){APP}"), 0.45),
    ("moving", re.compile(rf"(?i:\bmoving\s+away\s+from\s+){APP}"), 0.65),
    ("shutdown", re.compile(rf"{APP}(?i:\s+(?:is|was)\s+{FILLER}shutting\s+down)"), 0.90),
    ("dead", re.compile(rf"{APP}(?i:\s+(?:is|was|seems)\s+{FILLER}(?:dead|gone)\b)"), 0.80),
    ("disc", re.compile(rf"{APP}(?i:\s+(?:is\s+)?{FILLER}discontinued)"), 0.90),
    ("rip", re.compile(rf"(?i:\bRIP\s+){APP}"), 0.70),
    ("eol", re.compile(rf"{APP}\s+EOL\b"), 0.85),
]

# Confidence threshold above which a candidate is accepted regardless of
# catalog membership.  At/above this, shutdown vocab is the strong signal.
HIGH_CONFIDENCE = 0.80


def find_all(text: str) -> list[tuple[str, str, str, float]]:
    """Run every pattern over `text`, return tuples of
    (signal_type, signal_text, raw_candidate, base_confidence)."""
    out: list[tuple[str, str, str, float]] = []
    for stype, pat, conf in PATTERNS:
        for m in pat.finditer(text):
            candidate = m.group("app")
            out.append((stype, m.group(0), candidate, conf))
    return out
