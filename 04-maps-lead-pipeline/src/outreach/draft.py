"""Draft the introduction email per company.

Two drafters share one interface:
  - AnthropicDrafter: LLM draft (prompt caching + retry ladder).
  - TemplateDrafter: deterministic, no-API fallback. Used offline (smoke test)
    and whenever no Anthropic credential is set. Guaranteed lint-clean.

`get_drafter()` picks the right one. Both return (subject, body) or None on
permanent failure.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ..config import Settings
from ..models import Company
from .lint import EMOJI_RE

logger = logging.getLogger(__name__)

DRAFT_FAILED_SENTINEL = None
_RETRY_DELAYS = (1.0, 2.0, 4.0)

# Trade name per vertical, as it reads in a sentence ("for plumbing in
# Springfield"). Keys match config/verticals.yml.
_TRADE = {
    "roofing": "roofing",
    "plumbing": "plumbing",
    "electrical": "electrical work",
    "painting": "painting",
}

# Certs worth naming back to the recipient. Standards and safety schemes are
# detected (they still score) but never mentioned.
ALLOWED_CERTS = ("ISO 9001",)

# A/B subject test: the two variants alternate within a batch.
SUBJECTS = {
    "A": "A question about your service area in {city}",
    "B": "Working together on {trade} projects",
}


def clean_company_name(name: str) -> str:
    """Company name as a person would write it: no emoji, no "| tagline" suffix."""
    name = EMOJI_RE.sub("", name.split("|", 1)[0])
    return " ".join(name.split())


def build_fields(
    company: Company,
    verticals_cfg: dict,
    settings: Settings,
    *,
    subject_variant: str = "A",
) -> dict:
    verts = company.verticals_served or []
    vertical = verts[0] if verts else "generic"
    label = verticals_cfg.get(vertical, {}).get("label", "Trades")
    trade = _TRADE.get(vertical, "trade services")
    cert = next((c for c in company.certs if c in ALLOWED_CERTS), "")
    name = clean_company_name(company.name)
    if company.city:
        subject = SUBJECTS[subject_variant].format(city=company.city, trade=trade)
    else:  # variant A without a place reads "A question about your service area"
        subject = SUBJECTS[subject_variant].replace(" in {city}", "").format(trade=trade)
    return {
        "company_name": name,
        "city": company.city or "your region",
        "vertical_label": label,
        "trade": trade,
        "cert": cert,
        "cert_mention": cert or "(no certification detected; do not mention)",
        "subject": subject,
        "subject_variant": subject_variant,
        "sender_block": settings.sender_block(name),
    }


# The contract in email_prompt.md is two header lines and then the email:
#     Subject: <subject line>
#     ---
#     <body>
# Splitting on the first "---" ANYWHERE would keep only what follows, so a model
# that put a horizontal rule above its sign-off would lose the whole pitch -- a
# two-line draft that lints clean and exports. Anchor on the subject line
# instead: the body is everything after it, and a rule is dropped only when it
# is the body's own first line, which is exactly where the contract puts it.
_SUBJECT_RE = re.compile(r"^[ \t]*subject[ \t]*:[ \t]*(.+?)[ \t]*$", re.I | re.M)
_LEADING_RULE_RE = re.compile(r"\A\s*(?:-{3,}|\*{3,}|_{3,})[ \t]*(?:\r?\n|\Z)")


def _parse_llm(text: str) -> tuple[str, str]:
    subject, body = "", text
    m = _SUBJECT_RE.search(text)
    if m:
        subject = m.group(1).strip()
        body = text[m.end():]
    body = _LEADING_RULE_RE.sub("", body).strip()
    return subject or "Working together", body


class TemplateDrafter:
    """Deterministic draft. No network. Always lint-clean."""

    name = "template"
    network_bound = False  # offline; never throttled

    async def draft_one(self, company: Company, fields: dict) -> tuple[str, str]:
        cert_line = ""
        if fields["cert"]:
            cert_line = (
                f"We noticed on your website that you work with {fields['cert']}.\n\n"
            )
        body = (
            "Dear Sir or Madam,\n\n"
            f"We came across {fields['company_name']} while looking for "
            f"{fields['trade']} specialists in {fields['city']}, and would like "
            "to introduce ourselves.\n\n"
            f"{cert_line}"
            "If you are open to exploring a possible collaboration, a short reply "
            "is enough and we will suggest a brief introductory call at a time "
            "that suits you.\n\n"
            f"{fields['sender_block']}"
        )
        return fields["subject"], body


class AnthropicDrafter:
    network_bound = True  # hits the Anthropic API; paced by draft_min_interval_seconds

    def __init__(self, settings: Settings, system_prompt: str, user_template: str) -> None:
        from anthropic import AsyncAnthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("No Anthropic credential set")
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.draft_model
        self._system = system_prompt
        self._user_template = user_template
        self.name = "anthropic"

    def _system_blocks(self) -> list[dict]:
        # The system prompt is identical for every company, so cache it.
        return [
            {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
        ]

    async def draft_one(self, company: Company, fields: dict) -> tuple[str, str] | None:
        from anthropic import (
            APIConnectionError,
            APIError,
            APIStatusError,
            RateLimitError,
        )

        user = self._user_template.format(**fields)
        for delay in (*_RETRY_DELAYS, None):
            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=self._system_blocks(),
                    messages=[{"role": "user", "content": user}],
                )
                parts = [
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                ]
                subject, body = _parse_llm("\n".join(parts).strip())
                # The subject is the A/B arm, not the model's to rewrite.
                return fields.get("subject") or subject, body
            # APIConnectionError covers APITimeoutError (its subclass) and the
            # plain connection failures -- DNS, reset, refused -- that would
            # otherwise fall past APIStatusError into the terminal `except
            # APIError` and burn the draft on one blip.
            except (RateLimitError, APIConnectionError) as e:
                if delay is None:
                    logger.error(f"draft: transient error exhausted for {company.name}: {e}")
                    return DRAFT_FAILED_SENTINEL
                await asyncio.sleep(delay)
            except APIStatusError as e:
                status = getattr(e, "status_code", 0)
                if 500 <= status < 600 and delay is not None:
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"draft: API status error for {company.name}: {e}")
                return DRAFT_FAILED_SENTINEL
            except APIError as e:
                logger.error(f"draft: API error for {company.name}: {e}")
                return DRAFT_FAILED_SENTINEL
        return DRAFT_FAILED_SENTINEL


def get_drafter(settings: Settings, system_prompt: str, user_template: str):
    """Anthropic when credentials exist and drafting is enabled, else template."""
    if settings.draft_enabled and settings.has_anthropic_auth:
        try:
            return AnthropicDrafter(settings, system_prompt, user_template)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"draft: falling back to template drafter ({e})")
    return TemplateDrafter()
