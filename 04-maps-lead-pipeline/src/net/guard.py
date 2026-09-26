"""SSRF guard for crawl targets.

The crawler follows links and redirects from arbitrary third-party websites, so
every hop is attacker-controlled: anyone can list a business and point their site
wherever they like. This module answers one question — may we send a request to
this URL? The scheme must be http(s), and the host must not resolve to a
non-global address (loopback, RFC1918 private, link-local, which is where the
169.254.169.254 cloud-metadata endpoint lives).

Checking only the URL the crawl started from is not enough: `follow_redirects`
made the robots.txt decision on host A while the fetch landed on host B. See
`net.http._send`, which revalidates every hop.

NOT a defence against DNS rebinding: we resolve, decide, and httpx resolves
again when it connects. Closing that gap needs connect-time address pinning.
This closes the practical vector — a link or redirect aimed at an internal
address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from ..scraper.retry import ChallengeDetected

ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedTarget(ChallengeDetected):
    """This URL must not be fetched.

    Subclasses ChallengeDetected so it inherits the "never retry"
    handling: `retry.is_retryable_error` refuses it, and callers that already
    catch ChallengeDetected skip the target gracefully instead of crashing the
    run.
    """


# host -> may we fetch it. Hosts repeat heavily (up to enrich_max_pages_per_site
# requests each), so one resolution per host per run keeps the DNS cost nil.
_host_allowed: dict[str, bool] = {}


def _all_global(ips: set[str]) -> bool:
    """True only if every resolved address is publicly routable."""
    if not ips:
        return False
    for ip in ips:
        try:
            if not ipaddress.ip_address(ip).is_global:
                return False
        except ValueError:
            return False
    return True


async def check_url(url: str) -> None:
    """Raise BlockedTarget unless `url` is a safe public http(s) target."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedTarget(f"scheme not allowed: {scheme or '(none)'} in {url}")
    try:
        host = parts.hostname
    except ValueError as e:  # malformed authority, e.g. a bad IPv6 literal
        raise BlockedTarget(f"unparseable host in {url}: {e}") from e
    if not host:
        raise BlockedTarget(f"no host in {url}")

    cached = _host_allowed.get(host)
    if cached is True:
        return
    if cached is False:
        raise BlockedTarget(f"host is not publicly routable: {host}")

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError as e:
        raise BlockedTarget(f"cannot resolve {host}: {e}") from e
    # sockaddr[0] is the address; str() because getaddrinfo is typed str | int.
    ips = {str(info[4][0]) for info in infos}
    allowed = _all_global(ips)
    _host_allowed[host] = allowed
    if not allowed:
        raise BlockedTarget(f"host is not publicly routable: {host} -> {sorted(ips)}")
