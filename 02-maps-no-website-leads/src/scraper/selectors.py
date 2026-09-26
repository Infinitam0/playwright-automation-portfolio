"""Every selector and JSON index path in the project lives here. Nowhere else.

Google rotates two things on independent, silent schedules:
  1. CSS class names (hfpxzc, Nv2PK, DUwDvf, qBF1Pd ...) — fast, weeks to months
  2. The index positions inside APP_INITIALIZATION_STATE — slow, but silent

Both are the entire long-term maintenance cost of this scraper. Concentrating
them in one file means a break is a one-file fix, and `scripts/canary.py` is what
tells you a break happened at all.

Each field is an ordered list of strategies tried in sequence. The JSON path is
always primary: it survives class rotation AND locale changes, whereas
aria-label selectors silently return nothing outside en-US.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Strategy types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonPath:
    """Walk a list of indices into the parsed APP_INITIALIZATION_STATE blob."""

    path: tuple[int, ...]

    def __init__(self, *indices: int) -> None:
        object.__setattr__(self, "path", tuple(indices))

    def extract(self, blob) -> str | float | int | None:
        cur = blob
        for i in self.path:
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
        return cur if cur not in ("", []) else None

    def __repr__(self) -> str:  # shows up in miss logs
        return f"json{list(self.path)}"


@dataclass(frozen=True)
class Dom:
    """CSS selector fallback. `attr=None` means take the element's text."""

    selector: str
    attr: str | None = None

    def __repr__(self) -> str:
        return f"dom({self.selector})"


# --------------------------------------------------------------------------
# PHASE 1 — search results feed
# --------------------------------------------------------------------------

# The scrollable results column. `role="feed"` has outlived every class name
# around it; never match on the sibling classes (m6QErb DxyBCb kA9KIf ...).
FEED = 'div[role="feed"]'

# Place link harvest. Union of two strategies — the first is what the actively
# maintained Go scraper uses, the second is semantic and independently stable.
# Taking the union costs nothing and means one rotating away is not an outage.
FEED_PLACE_LINKS = (
    'div[role="feed"] div[jsaction] > a',
    'div[role="feed"] a[href*="/maps/place/"]',
)

# A search that resolves to exactly one business skips the feed entirely and
# lands straight on the detail pane. Detect that rather than reporting 0 results.
SINGLE_PLACE_URL_MARKER = "/maps/place/"

# Scroll pacing.
#
# These numbers were wrong on the first live sweep and it cost real coverage:
# starting at 100ms with a 1.5x backoff meant three plateau rounds elapsed in
# ~475ms total, but Google fetches the next batch of results over the network on
# scroll, which takes 1-2s. Tiles were being declared exhausted before the first
# batch arrived — three of them returned exactly 10 results in ~1.5s while
# genuine tiles took ~48s and returned 120.
#
# The floor matters more than the growth rate: no plateau can now be concluded in
# under ~3.2s of quiet. Costs a few seconds on genuinely sparse tiles, which is
# far cheaper than silently missing businesses.
SCROLL_BACKOFF_START_MS = 800
SCROLL_BACKOFF_FACTOR = 1.3
SCROLL_BACKOFF_MAX_MS = 2500
SCROLL_PLATEAU_ROUNDS = 3  # no NEW results this many rounds => done

# A tile returning at or under this, having loaded quickly, is suspicious rather
# than sparse — worth a log line so truncation cannot hide again.
SUSPICIOUS_YIELD = 12

# Google caps a single search here. Hitting it means the tile had more to give.
RESULT_CAP = 120
SUBDIVIDE_THRESHOLD = 110


# --------------------------------------------------------------------------
# PHASE 2 — place detail
# --------------------------------------------------------------------------

# APP_INITIALIZATION_STATE holds XSSI-guarded JSON strings. Their location moves:
# published guides say state[3][6], but as of this build the payload sits at
# state[3]["qg"][2] — an object key, not an array index. So do not hardcode a
# path. Collect every guarded string in the state and let the caller pick the one
# that actually contains a place record.
XSSI_GUARD = ")]}'"

# The payload is injected asynchronously, well after domcontentloaded. Poll for
# it rather than sleeping a fixed amount: a fixed sleep is either too short (an
# empty result that looks exactly like "no data") or wasted time on every page.
HAS_BLOB_JS = r"""
() => {
  const seen = new Set();
  const walk = (node, depth) => {
    if (depth > 6) return false;
    if (typeof node === 'string') return node.startsWith(")]}'");
    if (!node || typeof node !== 'object' || seen.has(node)) return false;
    seen.add(node);
    const keys = Array.isArray(node) ? node.map((_, i) => i) : Object.keys(node);
    for (const k of keys) {
      try { if (walk(node[k], depth + 1)) return true; } catch (e) {}
    }
    return false;
  };
  return walk(window.APP_INITIALIZATION_STATE, 0);
}
"""

EXTRACT_BLOBS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const walk = (node, depth) => {
    if (depth > 6 || out.length > 40) return;
    if (typeof node === 'string') {
      if (node.startsWith(")]}'")) out.push(node);
      return;
    }
    if (!node || typeof node !== 'object' || seen.has(node)) return;
    seen.add(node);
    const keys = Array.isArray(node) ? node.map((_, i) => i) : Object.keys(node);
    for (const k of keys) {
      try { walk(node[k], depth + 1); } catch (e) {}
    }
  };
  walk(window.APP_INITIALIZATION_STATE, 0);
  return out.sort((a, b) => b.length - a.length);
}
"""

# Candidate paths to the place record INSIDE a parsed payload, newest first.
# The record's own field offsets have been stable for years; only its position in
# the enclosing payload moves. Verified live 2026-07-25 at [0][1][0][14]; [6] is
# the legacy location every published guide still cites.
RECORD_ROOTS = (
    (0, 1, 0, 14),
    (6,),
    (0, 1, 0, 15),
)

# All offsets below are RELATIVE TO THE PLACE RECORD, not the payload root.
# Verified live 2026-07-25 against a well-known chain store listing.

# THE field. Absent/null here is the whole product.
# [7][1] is the display domain ("example.com") and is a useful cross-check:
# a domain present with no full URL still means the business has a site.
WEBSITE = [
    JsonPath(7, 0),
    Dom('a[data-item-id="authority"]', attr="href"),
    Dom('a[aria-label^="Website: "]', attr="href"),
]
WEBSITE_DOMAIN = [JsonPath(7, 1)]

NAME = [
    JsonPath(11),
    Dom("h1"),
]

ADDRESS = [
    JsonPath(18),
    Dom('button[data-item-id="address"]', attr="aria-label"),
    Dom('*[aria-label^="Address: "]', attr="aria-label"),
]

PHONE = [
    JsonPath(178, 0, 0),
    Dom('button[data-item-id^="phone:tel:"]', attr="aria-label"),
    Dom('*[aria-label^="Phone: "]', attr="aria-label"),
]

CATEGORY = [
    JsonPath(13, 0),
    Dom('button[jsaction*="category"]'),
]

PLACE_ID = [JsonPath(78)]
RATING = [JsonPath(4, 7)]
REVIEW_COUNT = [JsonPath(4, 8)]
PLUS_CODE = [JsonPath(183, 2, 2, 0)]
LATITUDE = [JsonPath(9, 2)]
LONGITUDE = [JsonPath(9, 3)]

# Opening hours are NOT in this payload — verified 2026-07-25, no day names
# anywhere in the record; Maps fetches them lazily on demand. Published guides
# cite [203][0] and [34][1]; both are absent now.
#
# Left unextracted on purpose. A field that always misses would add a permanent
# entry to every record's extraction_misses, and that list is the rot alarm —
# a constant false positive in it would train us to ignore the one signal that
# tells us Google moved the offsets. Hours are not needed to pitch anyone.
HOURS: list = []

# The record carries its own feature id — authoritative, unlike the URL regex.
RECORD_FID = [JsonPath(10)]

# An unclaimed listing is a strong buying signal — nobody is managing it.
CLAIM_LINK = [
    Dom('a[href*="business.google.com"]', attr="href"),
    Dom('*[aria-label*="Claim this business"]', attr="aria-label"),
]

# Aria-label fallbacks above are English-only. Every URL must pin this or they
# silently return nothing — and it forces the consent buttons into English.
LOCALE_PARAMS = "hl=en&gl=us"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

# Feature ID: 0x<map feature>:0x<listing>. Present in the place href, so it is
# extractable in phase 1 — before spending a detail page load on a duplicate.
FID_RE = re.compile(r"(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)

# Fallback when a href carries no FID.
LATLNG_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def extract_fid(url: str) -> str | None:
    m = FID_RE.search(url or "")
    return m.group(1).lower() if m else None


def looks_like_place_record(rec) -> bool:
    """Is this list a place record?

    Validated by evidence rather than position: a real record carries a feature
    id at [10] and a name at [11]. Checking both makes a false positive very
    unlikely, which is what lets the locator below search blindly when the known
    paths stop working.
    """
    if not isinstance(rec, list) or len(rec) < 20:
        return False
    fid, name = rec[10], rec[11]
    return bool(
        isinstance(name, str)
        and name.strip()
        and isinstance(fid, str)
        and FID_RE.fullmatch(fid)
    )


def find_place_record(blob):
    """Locate the place record in a parsed payload.

    Tries the known paths first, then falls back to a bounded recursive search.
    The fallback is the whole point: when Google reshuffles the payload again,
    this keeps working and the caller logs that it had to fall back.
    """
    for path in RECORD_ROOTS:
        cur = blob
        for i in path:
            if not isinstance(cur, list) or i >= len(cur):
                cur = None
                break
            cur = cur[i]
        if looks_like_place_record(cur):
            return cur, list(path)

    stack = [(blob, [])]
    while stack:
        node, path = stack.pop()
        if len(path) > 8:
            continue
        if looks_like_place_record(node):
            return node, path
        if isinstance(node, list):
            for i, child in enumerate(node[:60]):
                if isinstance(child, list):
                    stack.append((child, path + [i]))
    return None, None


def fid_to_cid(fid: str) -> str | None:
    """Second half of the FID pair, hex -> decimal. This is the CID: a stable
    listing id that survives the business changing name or moving address."""
    try:
        return str(int(fid.split(":")[1], 16))
    except (ValueError, IndexError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------

CONSENT_FORM = 'form[action*="consent.google"]'
# Reject rather than accept — safer GDPR posture, same practical outcome.
CONSENT_REJECT_TEXTS = (
    "Reject all",
    "Alles afwijzen",  # nl
    "Alle ablehnen",  # de
    "Tout refuser",  # fr
    "Rechazar todo",  # es
)
