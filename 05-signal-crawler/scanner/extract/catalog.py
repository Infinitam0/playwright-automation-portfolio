"""Known-app catalog for disambiguation.

Loaded from `known_apps.yaml` at the project root, which is maintained by
hand. The AlternativeTo scraper reads it to pick the app pages to check.

The catalog has one job: given a raw candidate string like 'wunderlist' or
'Inbox', return either the canonical name (e.g. 'Wunderlist', 'Google Inbox')
or None when no confident match exists. This is what kills false positives
like "alternative to inbox" matching the word "inbox".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

# Lowercase candidates we never accept under low-confidence patterns. Real apps
# with these names (Inbox, Mailbox, Clipboard) reach `apps` only via shutdown
# vocab (e.g. "Inbox is shutting down"), not "alternative to inbox".
STOP_WORDS: frozenset[str] = frozenset(
    {
        "it",
        "this",
        "that",
        "them",
        "they",
        "anything",
        "something",
        "everything",
        "everyone",
        "one",
        "all",
        "any",
        "some",
        "many",
        "few",
        "inbox",
        "mailbox",
        "clipboard",
        "the",
    }
)

# Abstract concepts and metaphorical nouns that frequently appear with
# shutdown vocab ("X is dead", "X is discontinued") but are never real apps.
# Applied to BOTH low and high confidence — the recall trade-off is worth it
# because these single-mention metaphors dominate output otherwise. Add
# entries as they surface during validation runs.
METAPHORICAL: frozenset[str] = frozenset(
    {
        # philosophy / sociology terms commonly declared "dead"
        "life",
        "love",
        "art",
        "music",
        "journalism",
        "satire",
        "irony",
        "civility",
        "decency",
        "trust",
        "privacy",
        "consent",
        "youth",
        "innocence",
        "manhood",
        "childhood",
        "history",
        "truth",
        "democracy",
        "capitalism",
        "communism",
        "liberty",
        "free speech",
        # tech buzzwords and meta-concepts
        "agile",
        "agility",
        "scrum",
        "waterfall",
        "ai",
        "blockchain",
        "crypto",
        "metaverse",
        "tooling",
        "code",
        "software engineering",
        "computer science",
        "open source",
        "free software",
        # generic platforms / abstractions
        "the internet",
        "the web",
        "the cloud",
        "the news",
        "the past",
        "the future",
        "the old days",
        # spam-trap nouns
        "spam",
        "phishing",
        "noise",
        "hype",
        # known-grok-ish but ambiguous (filter for v1; revisit if real)
        "grok",
        # ASIC etc. — capitalised acronyms that read as proper nouns
        "asic",
    }
)


def _looks_like_article_phrase(candidate: str) -> bool:
    """Candidates beginning with an article ('The PHP License', 'A Day In...')
    are almost never app names. Apps drop the article in their canonical name."""
    head = candidate.split(None, 1)[0].lower() if candidate else ""
    return head in {"the", "a", "an"}


@dataclass
class CatalogEntry:
    name: str
    aliases: list[str] = field(default_factory=list)
    platform: str = "unknown"
    discontinued: bool = False


class Catalog:
    """Loaded once, queried many times. Add new apps at runtime via add()."""

    def __init__(self, entries: list[CatalogEntry] | None = None) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        self._alias_to_canonical: dict[str, str] = {}
        for e in entries or []:
            self._index(e)

    @classmethod
    def load(cls, yaml_path: str | Path = "known_apps.yaml") -> Catalog:
        path = Path(yaml_path)
        if not path.exists():
            return cls([])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries: list[CatalogEntry] = []
        for app in data.get("apps", []) or []:
            entries.append(
                CatalogEntry(
                    name=app["name"],
                    aliases=list(app.get("aliases", []) or []),
                    platform=app.get("platform", "unknown"),
                    discontinued=str(app.get("status", "")).lower() == "discontinued",
                )
            )
        return cls(entries)

    # ---- mutation ----

    def add(self, entry: CatalogEntry) -> None:
        self._index(entry)

    def _index(self, entry: CatalogEntry) -> None:
        self._entries[entry.name] = entry
        names = {entry.name, *entry.aliases}
        for n in names:
            self._alias_to_canonical[n.lower()] = entry.name

    # ---- queries ----

    def is_discontinued(self, canonical_name: str) -> bool:
        entry = self._entries.get(canonical_name)
        return bool(entry and entry.discontinued)

    def resolve(self, candidate: str, min_ratio: int = 88) -> str | None:
        """Return canonical name if `candidate` fuzzy-matches an alias.
        None otherwise. Case-insensitive, quote-stripped."""
        cleaned = candidate.strip().strip("\"'").lower()
        if not cleaned:
            return None
        # Exact alias hit short-circuits the fuzzy scan.
        exact = self._alias_to_canonical.get(cleaned)
        if exact is not None:
            return exact
        if not self._alias_to_canonical:
            return None
        match = process.extractOne(
            cleaned,
            self._alias_to_canonical.keys(),
            scorer=fuzz.ratio,
        )
        if match is None:
            return None
        matched_alias, ratio, _ = match
        if ratio < min_ratio:
            return None
        return self._alias_to_canonical[matched_alias]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, canonical_name: str) -> bool:
        return canonical_name in self._entries


def disambiguate(
    raw_candidate: str,
    base_confidence: float,
    catalog: Catalog,
    high_confidence_threshold: float = 0.80,
) -> str | None:
    """The single disambiguation entry point used by the extractor.

    Rules:
    - Strip quotes/whitespace; reject empty.
    - Always reject STOP_WORDS, METAPHORICAL, and "The/A/An <X>" article phrases.
      Metaphorical filtering keeps "Life is dead" / "Agile is discontinued"
      noise out of the top-apps view — apps with these literal names would
      need to land in the catalog to surface.
    - High-confidence patterns: try the catalog; on miss, fall back to the
      cleaned candidate in TitleCase so the scanner can discover novel apps.
    - Low-confidence patterns: require a catalog hit.
    """
    cleaned = raw_candidate.strip().strip("\"'")
    if not cleaned:
        return None

    lc = cleaned.lower()
    # Hard rejects regardless of catalog: metaphorical nouns and article
    # phrases are never app names ("Life is dead", "The PHP License is...").
    if lc in METAPHORICAL or _looks_like_article_phrase(cleaned):
        return None

    canonical = catalog.resolve(cleaned)

    if base_confidence >= high_confidence_threshold:
        # High confidence: catalog hit wins (even for stop-words like
        # "inbox" -> "Google Inbox"). Otherwise reject stop-words and
        # fall back to TitleCased candidate.
        if canonical is not None:
            return canonical
        if lc in STOP_WORDS:
            return None
        return cleaned if cleaned[0].isupper() else cleaned.title()

    # Low confidence: catalog must vouch, AND stop-words are out even on
    # a catalog hit ("alternative to inbox" is the word, not the app).
    if lc in STOP_WORDS:
        return None
    return canonical
