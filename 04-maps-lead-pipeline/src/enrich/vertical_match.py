"""Confirm which configured verticals a company actually serves, from site text,
and flag out-of-scope / wrong-intent companies via the negative-keyword list."""

from __future__ import annotations

import re

from ..models import VERTICALS


def match_verticals(text: str, verticals_cfg: dict) -> list[str]:
    low = text.lower()
    matched: list[str] = []
    for vertical in VERTICALS:  # only the real verticals, not "generic"
        cfg = verticals_cfg.get(vertical, {})
        for kw in cfg.get("site_keywords", []):
            kwl = kw.lower()
            # Short tokens (e.g. "ac") need word boundaries so they don't hit
            # inside unrelated words; longer keywords stay plain substrings.
            if len(kwl) <= 3:
                if re.search(rf"\b{re.escape(kwl)}\b", low):
                    matched.append(vertical)
                    break
            elif kwl in low:
                matched.append(vertical)
                break
    return matched


# How close a target-segment signal has to sit to a negative hit to read as part
# of the same statement, and how many times it has to appear to count as more
# than a passing mention. See `negative_keyword_hit` for why both exist.
SEGMENT_WINDOW_CHARS = 300
SEGMENT_MIN_HITS = 2


def _word_hit(haystack: str, terms: list[str]) -> str:
    # Word-boundary match so a short word like "diy" only fires when it stands
    # alone (not inside another word), while multiword phrases still match.
    for kw in terms:
        if re.search(rf"\b{re.escape(kw)}\b", haystack, re.IGNORECASE):
            return kw
    return ""


def _word_positions(haystack: str, terms: list[str]) -> list[tuple[int, str]]:
    """Every word-boundary match of every term, as (offset, term), in order."""
    found: list[tuple[int, str]] = []
    for kw in terms:
        for m in re.finditer(rf"\b{re.escape(kw)}\b", haystack, re.IGNORECASE):
            found.append((m.start(), kw))
    found.sort()
    return found


def negative_keyword_hit(
    text: str,
    name: str,
    negatives: list[str],
    segment_signals: list[str] | None = None,
) -> str:
    """The wrong-segment term that disqualifies this company, else "".

    A negative term alone is not disqualifying: companies routinely advertise
    several markets ("we supply the trade and take on private jobs"), and one
    that also serves the target segment is a valid lead however much other
    work it does.

    But "any segment signal anywhere cancels the hit" made the veto nearly
    inert: common phrases turn up in a blog post or a footer, so almost
    nothing was ever disqualified. Reverting to the bare list is not the answer
    either -- on a real run it suppressed hundreds of otherwise top-tier
    companies.

    So the segment evidence has to be more than incidental. Either counts:

      * co-mention -- a signal within SEGMENT_WINDOW_CHARS of the hit, which
        is literally the "supply the trade and take on private jobs" sentence;
      * repetition -- SEGMENT_MIN_HITS or more occurrences anywhere, so a site
        that keeps saying who it serves is taken at its word.

    A single stray signal far from any negative term no longer cancels.
    """
    haystack = f"{name} {text}"
    hits = _word_positions(haystack, negatives)
    if not hits:
        return ""
    if not segment_signals:
        return hits[0][1]

    segment = _word_positions(haystack, segment_signals)
    if len(segment) >= SEGMENT_MIN_HITS:
        return ""
    for pos, _ in hits:
        if any(abs(spos - pos) <= SEGMENT_WINDOW_CHARS for spos, _ in segment):
            return ""
    return hits[0][1]
