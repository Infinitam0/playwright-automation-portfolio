"""Offline parser tests against a synthetic place record.

The record below is fake data laid out at the same offsets as a live Maps
payload (see selectors.py). These are what make an offset-rot break fast to
diagnose: when Google reshuffles the payload, these keep passing (the layout is
frozen here) while the canary fails against live pages. The difference between
the two tells you it is Google that changed, not the code.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scraper import selectors as S  # noqa: E402
from src.scraper.detail import parse_detail  # noqa: E402


def _synthetic_record() -> list:
    """A fake place record with every field at its live-payload offset."""
    rec: list = [None] * 184
    rec[4] = [None] * 7 + [4.6, 1234]  # [4][7] rating, [4][8] review count
    rec[7] = ["https://www.example-coffee.test/locations/main", "example-coffee.test"]
    rec[9] = [None, None, 45.1234, -93.5678]  # [9][2] lat, [9][3] lng
    rec[10] = "0x0000000000000abc:0x0000000000000def"
    rec[11] = "Example Coffee Roastery"
    rec[13] = ["Coffee shop"]
    rec[18] = "1 Example Street, Example City"
    rec[78] = "ChIJ_FAKE_PLACE_ID_0000"
    rec[178] = [["+1 555-010-0123"]]
    rec[183] = [None, None, [None, None, ["XXXX+XX Example City"]]]
    return rec


@pytest.fixture
def record():
    return _synthetic_record()


def test_parses_the_website(record):
    d = parse_detail(record, "https://maps.google.com/x")
    assert d.website == "https://www.example-coffee.test/locations/main"


def test_parses_core_fields(record):
    d = parse_detail(record, "https://maps.google.com/x")
    assert d.name == "Example Coffee Roastery"
    assert d.phone == "+1 555-010-0123"
    assert "1 Example Street" in d.address
    assert d.category == "Coffee shop"
    assert d.place_id.startswith("ChIJ")
    assert d.rating == pytest.approx(4.6)
    assert d.review_count == 1234
    assert d.plus_code == "XXXX+XX Example City"
    assert d.latitude == pytest.approx(45.1234)
    assert d.longitude == pytest.approx(-93.5678)


def test_no_extraction_misses_on_a_complete_record(record):
    """A miss here means an offset moved — that is the alarm, so keep it clean."""
    d = parse_detail(record, "https://maps.google.com/x")
    assert d.extraction_misses == []


def test_fid_comes_from_the_record_not_the_url(record):
    d = parse_detail(record, "https://maps.google.com/no-fid-here")
    assert S.FID_RE.fullmatch(d.fid)
    assert d.cid and d.cid.isdigit()


def test_record_locator_finds_it_at_a_known_root(record):
    blob = [[None, [[None] * 14 + [record]]]]
    found, path = S.find_place_record(blob)
    assert found is record
    assert list(path) == [0, 1, 0, 14]


def test_record_locator_falls_back_to_search(record):
    """When Google moves the record, the blind search must still find it."""
    blob = [None, [None, [None, None, [None, record]]]]
    found, path = S.find_place_record(blob)
    assert found is record
    assert list(path) not in [list(p) for p in S.RECORD_ROOTS]


def test_record_locator_rejects_non_records():
    found, _ = S.find_place_record([["not", "a", "record"], [1, 2, 3]])
    assert found is None


def test_missing_website_is_not_an_extraction_miss(record):
    """A business with no site is a valid result, not a parse failure. If this
    lands in misses it pollutes the rot alarm and hides real breakage."""
    stripped = list(record)
    stripped[7] = None
    d = parse_detail(stripped, "https://maps.google.com/x")
    assert d.website is None
    assert "website" not in d.extraction_misses


def test_bare_domain_counts_as_having_a_website(record):
    """[7][1] holds a display domain. Missing the full URL but having a domain
    still means they have a site — omitting this would leak a false lead."""
    stripped = list(record)
    stripped[7] = [None, "example.com"]
    d = parse_detail(stripped, "https://maps.google.com/x")
    assert d.website == "https://example.com"


def test_truncated_record_does_not_raise(record):
    d = parse_detail(record[:20], "https://maps.google.com/x")
    assert d.extraction_misses  # it should complain, not crash
