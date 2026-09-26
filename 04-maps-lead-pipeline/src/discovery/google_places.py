"""Google Places API (New) — the primary, ToS-clean discovery source.

Uses `places:searchText` (v1). One request per search task, paginated up to
`places_max_pages`. Returns name, website, phone, address, rating, review count
and a stable place_id. Email is NOT exposed by Google — that comes from website
enrichment downstream.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from __future__ import annotations

import logging

from ..models import RawCandidate, Source
from ..net.http import HttpClient
from .base import DiscoverySource, SearchTask

logger = logging.getLogger(__name__)

_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)


class GooglePlacesSource(DiscoverySource):
    name = "places"

    def __init__(self, http: HttpClient, api_key: str, *, max_pages: int = 3,
                 language: str = "nl", region: str = "nl") -> None:
        if not api_key:
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY is not set — cannot use the Places source."
            )
        self._http = http
        self._api_key = api_key
        self._max_pages = max_pages
        self._language = language
        self._region = region

    async def search(self, task: SearchTask) -> list[RawCandidate]:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": _FIELD_MASK,
        }
        out: list[RawCandidate] = []
        page_token = ""
        for _ in range(self._max_pages):
            body: dict = {
                "textQuery": task.query,
                "languageCode": self._language,
                "regionCode": self._region.upper(),
            }
            if page_token:
                body["pageToken"] = page_token
            resp = await self._http.post(_ENDPOINT, json=body, headers=headers)
            data = resp.json()
            places = data.get("places", []) or []
            for p in places:
                cand = self._parse(p, task)
                if cand:
                    out.append(cand)
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
        logger.info(f"places: '{task.query}' -> {len(out)} candidates")
        return out

    def _parse(self, p: dict, task: SearchTask) -> RawCandidate | None:
        name = (p.get("displayName") or {}).get("text", "").strip()
        if not name:
            return None
        comps = p.get("addressComponents", []) or []
        city = _component(comps, "locality") or _component(comps, "postal_town")
        province = _component(comps, "administrative_area_level_1")
        postcode = _component(comps, "postal_code")
        # regionCode only *biases* ranking, so a query near a national border can
        # return firms from the neighbouring country. Record the real country and let
        # scoring veto anything outside the target market.
        country = _component(comps, "country", short=True)
        return RawCandidate(
            name=name,
            website_url=p.get("websiteUri", "") or "",
            phone=p.get("nationalPhoneNumber", "") or "",
            street=p.get("formattedAddress", "") or "",
            postcode=postcode,
            city=city or task.city,
            province=province or task.province,
            country=country,
            google_rating=p.get("rating"),
            google_review_count=p.get("userRatingCount"),
            google_place_id=p.get("id", "") or "",
            source=Source(
                name=self.name,
                source_id=p.get("id", "") or "",
                url=f"https://www.google.com/maps/place/?q=place_id:{p.get('id','')}",
            ),
        )


def _component(components: list[dict], type_name: str, *, short: bool = False) -> str:
    """Address component by type. `short` prefers shortText (country -> "NL")."""
    for c in components:
        if type_name in (c.get("types") or []):
            if short:
                return c.get("shortText") or c.get("longText") or ""
            return c.get("longText") or c.get("shortText") or ""
    return ""
