"""Extract + classify email addresses from a company website.

Prefers role addresses (info@, contact@, sales@) over named-personal ones —
role mailboxes are the GDPR-safer target for B2B outreach. Handles
mailto: links, plain addresses, and common obfuscation ([at]/(at), [dot], &#64;).
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from ..models import EmailHit

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.IGNORECASE)

# Includes the Dutch role mailboxes seen on target sites (verkoop = sales,
# offerte = quote, klantenservice = customer service, aanvraag = request).
_ROLE_LOCALPARTS = {
    "info", "contact", "sales", "verkoop", "offerte", "offertes", "mail",
    "hello", "hallo", "klantenservice", "administratie", "planning", "service",
    "welkom", "vraag", "aanvraag",
}

# Skip these — placeholders, asset hosts, transactional no-reply, tracking.
_JUNK_DOMAINS = {
    "example.com", "example.nl", "domain.com", "email.com", "sentry.io",
    "wix.com", "wixpress.com", "godaddy.com", "cloudflare.com", "schema.org",
    "sentry-next.wixpress.com", "w3.org", "googleapis.com", "gstatic.com",
}
_JUNK_LOCALPARTS = {"no-reply", "noreply", "your-email", "youremail", "name", "email"}

# Separators a mailbox uses to qualify a role word (info.north, sales-nl).
_LOCAL_TOKEN_RE = re.compile(r"[.\-_+]")
# common image/asset extensions that regex can mistake for a TLD
_ASSET_TAIL = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|css|js|ico)$", re.IGNORECASE)


# Whitespace runs are BOUNDED here on purpose. An unbounded `\s*` on both ends
# is quadratic: at every offset the engine consumes the whole whitespace run,
# fails the bracket, backs off one, and repeats. A page with a ~500k-char
# whitespace run took ~22 min inside one re.sub — and because enrichment is
# async while re.sub holds the GIL, that one page froze every worker. Real
# obfuscation ("info [at] example [dot] com") never spans more than a space or
# two, so {0,4} keeps the intent and makes the scan linear.
_AT_RE = re.compile(r"\s{0,4}[\[(]\s{0,4}at\s{0,4}[\])]\s{0,4}", re.IGNORECASE)
_DOT_RE = re.compile(r"\s{0,4}[\[(]\s{0,4}dot\s{0,4}[\])]\s{0,4}", re.IGNORECASE)


def _deobfuscate(text: str) -> str:
    t = text.replace("&#64;", "@").replace("&#46;", ".")
    t = _AT_RE.sub("@", t)
    t = _DOT_RE.sub(".", t)
    return t


def _classify(address: str) -> str:
    local = address.split("@", 1)[0].lower()
    # A role mailbox often carries a qualifier: info.north@, sales-nl@,
    # service_south@. Matching the whole local part alone read every one of those
    # as personal, and best_email() sorts role-first, so outreach was pushed
    # toward a named individual's mailbox -- the opposite of this module's stated
    # GDPR rationale. Any separated token counts.
    #
    # This is safe only while _ROLE_LOCALPARTS holds nothing that could be a
    # person's name: "john.service@" reading as role is the harmless direction,
    # but adding a word like "mark" or "wil" would make personal addresses rank
    # first, which is the direction that matters. Keep the set functional.
    if local in _ROLE_LOCALPARTS or (
        set(_LOCAL_TOKEN_RE.split(local)) & _ROLE_LOCALPARTS
    ):
        return "role"
    if "." in local or "-" in local:
        # firstname.lastname / john-smith => personal-ish
        return "personal"
    return "unknown"


def _valid(address: str) -> bool:
    address = address.strip().strip(".,;:")
    if _ASSET_TAIL.search(address):
        return False
    if "@" not in address:
        return False
    local, _, domain = address.partition("@")
    if domain.lower() in _JUNK_DOMAINS or local.lower() in _JUNK_LOCALPARTS:
        return False
    if "." not in domain:
        return False
    return True


def extract_emails(html: str, *, source_page: str = "") -> list[EmailHit]:
    found: dict[str, EmailHit] = {}

    def add(addr: str, confidence: float) -> None:
        # A mailto: capture runs to the next quote, so it carried whatever the
        # page put there: "info@x.com\" (JSON-escaped quote), "info@x.com&#039",
        # "info@x.com%20", "info@x.com/contact", "mail:info@x.com". About a hundred
        # stored addresses in one real run were unsendable that way. Keep only
        # the address.
        m = _EMAIL_RE.search(unquote(addr))
        if not m:
            return
        addr = m.group(0).strip(".").lower()
        if not _valid(addr):
            return
        hit = found.get(addr)
        if hit is None or confidence > hit.confidence:
            found[addr] = EmailHit(
                address=addr,
                kind=_classify(addr),
                confidence=confidence,
                source_page=source_page,
            )

    for m in _MAILTO_RE.findall(html):
        add(m, 0.9)
    for m in _EMAIL_RE.findall(html):
        add(m, 0.7)
    for m in _EMAIL_RE.findall(_deobfuscate(html)):
        add(m, 0.6)

    return sorted(
        found.values(),
        key=lambda e: (e.kind == "role", e.confidence),
        reverse=True,
    )
