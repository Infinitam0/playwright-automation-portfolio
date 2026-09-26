"""Self-check for event parsing. Run: python 10-itsm-export-assign/test_scrape_export.py"""

from datetime import datetime

from models import EventType
from scrape_export import classify_event, parse_event_timestamp


def test_parse_event_timestamp() -> None:
    assert parse_event_timestamp("Unassigned by J. Doe 01-01-2000 00:00") == datetime(2000, 1, 1, 0, 0)
    assert parse_event_timestamp("2 January 2000 9:05 assigned") == datetime(2000, 1, 2, 9, 5)
    assert parse_event_timestamp("31-02-2000 10:00") is None  # invalid date
    assert parse_event_timestamp("no timestamp here") is None


def test_classify_event() -> None:
    assert classify_event("Person UNASSIGNED from asset") is EventType.UNASSIGNED
    assert classify_event("Person assigned to asset") is EventType.ASSIGNED
    assert classify_event("Asset renamed") is None


if __name__ == "__main__":
    test_parse_event_timestamp()
    test_classify_event()
    print("ok")
