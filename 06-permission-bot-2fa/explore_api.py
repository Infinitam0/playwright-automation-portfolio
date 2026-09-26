"""
Explore the portal's Swagger API endpoints (OAuth2 client credentials).

Two modes:
  - Default (requests-based): Authenticate via OAuth2, fetch Swagger spec,
    list and test endpoints related to users/permissions/access.
  - --browse: Open Swagger UI in a browser with bearer token pre-set
    (original Playwright-based mode for manual exploration).

Usage:
    python explore_api.py              # requests-based discovery
    python explore_api.py --browse     # open Swagger in browser
"""
import argparse
import os
import sys
from pathlib import Path

import pyotp
import requests
from dotenv import load_dotenv

# Load .env
load_dotenv(Path(__file__).parent / ".env")

# API credential names (these are the service account / API credentials)
API_USERNAME_KEY = "API_CLIENT_ID"
API_TOKEN_KEY = "API_CLIENT_SECRET"
TOTP_SECRET_KEY = "PORTAL_TOTP_SECRET"

_BASE_URL = (os.environ.get("PORTAL_BASE_URL") or "https://portal.example.com").rstrip("/")
OAUTH2_URL = os.environ.get("API_TOKEN_URL") or f"{_BASE_URL}/oauth2/token"
# Base URL of the REST API (Swagger lives under it); keep the trailing slash
API_BASE_URL = os.environ.get("API_BASE_URL") or f"{_BASE_URL}/api/"

# Common Swagger spec paths to try
SWAGGER_SPEC_PATHS = [
    "swagger/v1/swagger.json",
    "swagger/v2/swagger.json",
    "swagger/swagger.json",
    "swagger.json",
    "api/swagger.json",
    "api-docs",
    "v1/swagger.json",
    "v2/swagger.json",
]

# Keywords to filter for relevant endpoints
RELEVANT_KEYWORDS = [
    "user", "permission", "account", "role", "access", "auth", "group",
]


def _oauth_error_code(parse_json):
    """Return only the OAuth2 `error` code from an error response (no body, no details)."""
    try:
        body = parse_json()
    except Exception:
        return "(non-JSON error response)"
    return body.get("error", "(no error code)") if isinstance(body, dict) else "(unexpected error body)"


def _summarize(resp):
    """Describe a response by shape only, so record data never reaches the console."""
    try:
        body = resp.json()
    except ValueError:
        return f"{len(resp.content)} bytes, non-JSON"
    if isinstance(body, dict):
        return f"object with keys {sorted(body)[:10]}"
    if isinstance(body, list):
        return f"list of {len(body)} items"
    return type(body).__name__


def get_api_credentials():
    """Load API credentials from environment variables."""
    username = os.environ.get(API_USERNAME_KEY)
    api_token = os.environ.get(API_TOKEN_KEY, "").strip()
    totp_secret = os.environ.get(TOTP_SECRET_KEY, "").strip().replace(" ", "")

    missing = []
    if not username:
        missing.append(API_USERNAME_KEY)
    if not api_token:
        missing.append(API_TOKEN_KEY)
    if not totp_secret:
        missing.append(TOTP_SECRET_KEY)

    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return username, api_token, totp_secret


def authenticate_oauth2_requests(username, api_token, totp_secret):
    """Authenticate via OAuth2 using requests library. Returns (access_token, api_base_url)."""
    # The portal expects the client secret combined with a one-time (TOTP) code
    totp_code = pyotp.TOTP(totp_secret).now()
    client_secret = api_token + totp_code

    print(f"Authenticating as {username} via OAuth2...")
    resp = requests.post(OAUTH2_URL, data={
        "grant_type": "client_credentials",
        "client_id": username,
        "client_secret": client_secret,
    }, timeout=30)

    if resp.status_code != 200:
        print(f"OAuth2 FAILED: {resp.status_code} {_oauth_error_code(resp.json)}")
        sys.exit(1)

    data = resp.json()
    print(f"OAuth2 OK. Response keys: {list(data.keys())}")
    return data.get("access_token"), API_BASE_URL


def fetch_swagger_spec(api_base_url, access_token):
    """Try common paths to find and fetch the Swagger/OpenAPI spec."""
    headers = {"Authorization": f"Bearer {access_token}"}
    base = api_base_url.rstrip("/")

    for path in SWAGGER_SPEC_PATHS:
        url = f"{base}/{path}"
        print(f"  Trying: {url} ...", end=" ")
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "json" in content_type or path.endswith(".json"):
                    print("FOUND (JSON)")
                    return resp.json(), url
                else:
                    # Might be HTML Swagger UI - try to extract spec URL
                    text = resp.text
                    print(f"HTML ({len(text)} chars)")
                    # Look for spec URL in the Swagger UI HTML
                    for marker in ['url: "', "url: '", 'url:"', "url:'"]:
                        idx = text.find(marker)
                        if idx != -1:
                            end_char = marker[-1]
                            end_idx = text.find(end_char, idx + len(marker))
                            if end_idx != -1:
                                spec_url = text[idx + len(marker):end_idx]
                                if not spec_url.startswith("http"):
                                    spec_url = f"{base}/{spec_url.lstrip('/')}"
                                print(f"    Extracted spec URL: {spec_url}")
                                spec_resp = requests.get(spec_url, headers=headers, timeout=15)
                                if spec_resp.status_code == 200:
                                    return spec_resp.json(), spec_url
            else:
                print(f"{resp.status_code}")
        except requests.RequestException as e:
            print(f"ERROR: {e}")

    return None, None


def filter_relevant_endpoints(spec):
    """Extract endpoints matching relevant keywords from an OpenAPI spec."""
    paths = spec.get("paths", {})
    results = []

    for path, methods in paths.items():
        path_lower = path.lower()
        for method, details in methods.items():
            if method.startswith("x-"):
                continue  # Skip extensions
            summary = (details.get("summary") or "").lower()
            description = (details.get("description") or "").lower()
            tags = [t.lower() for t in details.get("tags", [])]
            all_text = f"{path_lower} {summary} {description} {' '.join(tags)}"

            if any(kw in all_text for kw in RELEVANT_KEYWORDS):
                results.append({
                    "method": method.upper(),
                    "path": path,
                    "summary": details.get("summary", ""),
                    "tags": details.get("tags", []),
                })

    return results


def test_endpoint(api_base_url, access_token, method, path):
    """Try calling an endpoint and return status + a content-free summary of the response."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base_url.rstrip('/')}{path}"

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=15)
        else:
            # For non-GET, just report the endpoint without calling
            return {"status": "SKIPPED", "reason": f"{method} not auto-tested"}

        return {"status": resp.status_code, "summary": _summarize(resp)}
    except requests.RequestException as e:
        return {"status": "ERROR", "error": str(e)}


def run_discovery():
    """Main discovery mode: authenticate, find spec, list and test endpoints."""
    username, api_token, totp_secret = get_api_credentials()
    access_token, api_base_url = authenticate_oauth2_requests(username, api_token, totp_secret)

    if not api_base_url:
        print("WARNING: No API base URL configured, cannot discover API.")
        sys.exit(1)

    print(f"\nAPI base URL: {api_base_url}")
    print()

    # Find Swagger spec
    print("=== Searching for Swagger/OpenAPI spec ===")
    spec, spec_url = fetch_swagger_spec(api_base_url, access_token)

    if spec is None:
        print("\nNo Swagger spec found at common paths.")
        print("Try --browse to open the Swagger UI in a browser manually.")
        sys.exit(1)

    print(f"\nSpec found at: {spec_url}")
    print(f"API title: {spec.get('info', {}).get('title', 'N/A')}")
    print(f"API version: {spec.get('info', {}).get('version', 'N/A')}")

    total_paths = len(spec.get("paths", {}))
    print(f"Total paths: {total_paths}")

    # List all endpoints
    print(f"\n=== All {total_paths} endpoints ===")
    for path, methods in sorted(spec.get("paths", {}).items()):
        for method in methods:
            if not method.startswith("x-"):
                summary = methods[method].get("summary", "")
                print(f"  {method.upper():6s} {path}  -- {summary}")

    # Filter relevant
    relevant = filter_relevant_endpoints(spec)
    print(f"\n=== Relevant endpoints ({len(relevant)}/{total_paths}) ===")
    for ep in relevant:
        print(f"  {ep['method']:6s} {ep['path']}")
        if ep["summary"]:
            print(f"         {ep['summary']}")
        if ep["tags"]:
            print(f"         tags: {ep['tags']}")

    # Test GET endpoints
    get_endpoints = [ep for ep in relevant if ep["method"] == "GET"]
    if get_endpoints:
        print(f"\n=== Testing {len(get_endpoints)} GET endpoints ===")
        for ep in get_endpoints:
            print(f"\n  GET {ep['path']}")
            result = test_endpoint(api_base_url, access_token, "GET", ep["path"])
            print(f"    Status: {result.get('status')}")
            if "summary" in result:
                print(f"    Response: {result['summary']}")
            elif "error" in result:
                print(f"    Error: {result['error']}")

    print("\n=== Discovery complete ===")


def run_browse():
    """Open Swagger UI in a browser with bearer token pre-set (original mode)."""
    from playwright.sync_api import sync_playwright

    username, api_token, totp_secret = get_api_credentials()

    # Client secret combined with a one-time (TOTP) code, as above
    totp_code = pyotp.TOTP(totp_secret).now()
    client_secret = api_token + totp_code

    print(f"OAuth2 login as {username}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()

        resp = context.request.post(OAUTH2_URL, form={
            "grant_type": "client_credentials",
            "client_id": username,
            "client_secret": client_secret,
        })

        if not resp.ok:
            print(f"OAuth2 FAILED: {resp.status} {_oauth_error_code(resp.json)}")
            browser.close()
            sys.exit(1)

        data = resp.json()
        api_base_url = API_BASE_URL
        access_token = data["access_token"]
        print(f"OK - api_base_url: {api_base_url}")

        context.set_extra_http_headers({
            "Authorization": f"Bearer {access_token}"
        })

        page = context.new_page()
        page.goto(api_base_url + "swagger")
        print(f"Browser open at {api_base_url}swagger")
        print("Explore manually. Press Enter here to close the browser.")
        input()

        browser.close()


def main():
    parser = argparse.ArgumentParser(description="Explore the portal Swagger API")
    parser.add_argument("--browse", action="store_true",
                        help="Open Swagger UI in a browser (requires Playwright)")
    args = parser.parse_args()

    if args.browse:
        run_browse()
    else:
        run_discovery()


if __name__ == "__main__":
    main()
