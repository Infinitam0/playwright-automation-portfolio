import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.classify import classify  # noqa: E402
from src.models import WebsiteTier  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [None, "", "   "],
)
def test_missing_url_is_none_tier(url):
    assert classify(url) is WebsiteTier.NONE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/somebiz",
        "http://facebook.com/somebiz",
        "https://m.facebook.com/somebiz",
        "https://www.instagram.com/example_nails/",
        "https://linktr.ee/somebiz",
        "https://mssg.me/example",
        "https://wa.me/15550100000",
    ],
)
def test_social_and_link_in_bio(url):
    assert classify(url) is WebsiteTier.SOCIAL_ONLY


@pytest.mark.parametrize(
    "url",
    [
        "http://examplenails.salonized.com/",
        "https://apnt.app/examplesalon",
        "https://www.thuisbezorgd.nl/menu/somebiz",
        "https://booksy.com/en-us/12345",
        "https://nl.ivof.com/000/",
    ],
)
def test_ordering_platforms(url):
    assert classify(url) is WebsiteTier.ORDERING_PLATFORM


@pytest.mark.parametrize(
    "url",
    [
        "https://example-barbershop.com/",
        "http://www.example-salon.org/",
        "https://www.example.com/locations/downtown",
        "example-salon.org",  # bare domain, no scheme
    ],
)
def test_real_websites(url):
    assert classify(url) is WebsiteTier.REAL


def test_subdomain_of_social_is_social():
    assert classify("https://business.facebook.com/x") is WebsiteTier.SOCIAL_ONLY


def test_lookalike_domain_is_not_social():
    """Substring matching would call this Facebook. It is not."""
    assert classify("https://notfacebook.com/x") is WebsiteTier.REAL
    assert classify("https://myfacebook.com.evil.net/x") is WebsiteTier.REAL


def test_google_builder_site_is_a_lead_by_default():
    assert classify("https://somebiz.business.site/") is WebsiteTier.SOCIAL_ONLY


def test_google_builder_site_configurable():
    assert (
        classify("https://somebiz.business.site/", builders_are_leads=False)
        is WebsiteTier.REAL
    )


def test_tracking_params_do_not_change_tier():
    assert (
        classify("https://www.example.com/store/main-street?cid=seo-maps")
        is WebsiteTier.REAL
    )


def test_extra_domains_from_config():
    assert (
        classify("https://weirdbuilder.io/x", extra_social={"weirdbuilder.io"})
        is WebsiteTier.SOCIAL_ONLY
    )


def test_malformed_url_does_not_raise():
    assert classify("http://[not a url") is WebsiteTier.NONE
