"""Enrichment: turn a discovered company + its website into a scored candidate."""

from __future__ import annotations

import re


def html_to_text(html: str) -> str:
    """Visible text from HTML (scripts/styles stripped). selectolax with a
    regex fallback so enrichment works even if the C extension is unavailable."""
    if not html:
        return ""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        body = tree.body or tree.root
        return body.text(separator=" ", strip=True) if body else ""
    except Exception:  # noqa: BLE001 - fallback
        no_scripts = re.sub(
            r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE
        )
        return re.sub(r"<[^>]+>", " ", no_scripts)
