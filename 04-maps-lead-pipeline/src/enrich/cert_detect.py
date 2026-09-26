"""Detect certifications from website text (a quality/legitimacy signal).

Patterns cover a few common standards and schemes; extend the list for your
trades and market.
"""

from __future__ import annotations

import re

_CERT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("NEN 1010", re.compile(r"nen\s*1010", re.I)),
    ("NEN 3140", re.compile(r"nen\s*3140", re.I)),
    ("VCA", re.compile(r"\bvca\b", re.I)),
    ("ISO 9001", re.compile(r"iso\s*9001", re.I)),
]


def detect_certs(text: str) -> list[str]:
    return [label for label, pat in _CERT_PATTERNS if pat.search(text)]
