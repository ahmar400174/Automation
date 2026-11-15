"""
MODULE STATUS
------------
The web app does NOT call this module. There is no Facebook token and no Graph request
on the verification path. The code remains only so the old unit tests of the HTTP client
still describe how the official API behaves. Do not wire it back in.

Official Meta Ad Library API: the EXACT number of ACTIVE ads of a page, for the countries Meta covers.

Why this exists
---------------
Reading the number from the Ad Library website needs a browser tab per page, and Meta throttles the IP after a
few hundred page loads (then the site even shows "No ads match your search criteria" for pages that DO run
ads). The official API has no such IP throttle: one request answers up to 10 pages, with no browser at all.

What it covers
--------------
Meta archives commercial ads only for the European Union and the United Kingdom, so the API answers FR, IT,
ES, NL, GB (and the other EU states). It does NOT hold United States commercial ads: those pages are still
checked in the browser. Every page the API cannot answer simply falls back to the browser checker.

Accuracy
--------
* The API returns one record per ACTIVE ad (ad_active_status=ACTIVE) delivered to the country, so the count
  is the real number of active ads, not the rounded "~N results" label of the website.
* A page with no active ad returns no record: that is an authoritative zero, but ONLY when the call itself
  succeeded. Any error (token, rate limit, network) leaves the page unanswered, never zero.

Setup (once, free): developers.facebook.com -> create an app, confirm the identity of the account at
facebook.com/ID, create a token (Graph API Explorer, then extend it to a 60 day token) and put it in the
Settings page (META_API_TOKEN). check_token() tells at once whether it works.
"""
import json
import os
import time

# European Union + United Kingdom: the only places where Meta archives ALL ads (not only political ones).
API_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
    "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB"})
BATCH = 10                      # page ids per request (Meta's limit for search_page_ids)
RATE_CODES = {4, 17, 32, 613}   # application / user request limit reached
AUTH_CODES = {102, 190, 200, 10, 2500}


class ApiAuthError(Exception):
    """The token is missing, expired or has no access to the Ad Library API."""


def supports(country):
    return str(country or "").strip().upper() in API_COUNTRIES


def token():
    return str(os.environ.get("META_API_TOKEN", "") or "").strip()


def _base():
    version = str(os.environ.get("META_API_VERSION", "v21.0") or "v21.0").strip()
    return f"https://graph.facebook.com/{version}/ads_archive"


def _sleep(seconds):
    time.sleep(seconds)


def _get(session, url, params, tries=6):
    """GET with backoff. Returns the decoded JSON, raises ApiAuthError for token problems, RuntimeError else."""
    delay = float(os.environ.get("META_API_BACKOFF", "20") or 20)
    last = "no attempt"
    for attempt in range(1, tries + 1):
        try:
            resp = session.get(url, params=params, timeout=60)
        except Exception as exc:                                   # network hiccup: wait and ask again
            last = f"{type(exc).__name__}: {exc}"[:160]
            _sleep(min(delay * attempt, 120))
            continue
        usage = resp.headers.get("x-app-usage") if getattr(resp, "headers", None) else None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            code = int(err.get("code") or 0)
            message = str(err.get("message") or "")[:200]
            if code in AUTH_CODES:
                raise ApiAuthError(f"Meta API refused the token (code {code}): {message}")
            if code in RATE_CODES or resp.status_code == 429:
                last = f"rate limit (code {code}): {message}"
                _sleep(min(delay * attempt * 2, 600))
                continue
            if code == 100 or resp.status_code in (400, 404):
                raise RuntimeError(f"Meta API rejected the request (code {code}): {message}")
            last = f"code {code}: {message}"
            _sleep(min(delay * attempt, 120))
            continue
        if resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            _sleep(min(delay * attempt, 120))
            continue
        if usage:                                                   # stay below the application limit
            try:
                if max(int(v) for v in json.loads(usage).values() if isinstance(v, (int, float))) >= 85:
                    _sleep(60)
            except (ValueError, TypeError):
                pass
        return data
    raise RuntimeError(f"Meta API did not answer: {last}")


def _ids_param(ids):
    # Graph accepts a plain comma separated list for array parameters.
    return ",".join(str(i) for i in ids)


def count_active(country, page_ids, access_token=None, session=None, limit=None):
    """{page_id: number of ACTIVE ads} for every page whose batch was answered completely (0 included).

    Pages of a batch that failed are NOT in the result, so the caller can verify them another way.
    """
    country = str(country).strip().upper()
    access_token = access_token or token()
    if not access_token:
        raise ApiAuthError("META_API_TOKEN is not set")
    if not supports(country):
        return {}
    if session is None:
        import requests
        session = requests.Session()
    limit = int(limit or os.environ.get("META_API_LIMIT", "500") or 500)
    gap = float(os.environ.get("META_API_GAP", "0.15") or 0)
    ids = [str(p) for p in dict.fromkeys(page_ids) if str(p).strip()]
    counts = {}
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        params = {
            "access_token": access_token,
            "ad_reached_countries": json.dumps([country]),
            "ad_active_status": "ACTIVE",
            "ad_type": "ALL",
            "search_page_ids": _ids_param(batch),
            "fields": "id,page_id",
            "limit": limit,
        }
        seen = {pid: set() for pid in batch}
        url, query, failed = _base(), params, False
        while url:
            try:
                data = _get(session, url, query)
            except ApiAuthError:
                raise
            except RuntimeError as exc:
                print(f"   (Meta API: batch of {len(batch)} page(s) not answered: {exc})", flush=True)
                failed = True
                break
            for ad in data.get("data") or []:
                pid = str(ad.get("page_id") or "")
                if pid in seen and ad.get("id"):
                    seen[pid].add(str(ad["id"]))               # one record per ad: a repeated id counts once
            url, query = (data.get("paging") or {}).get("next"), None   # "next" is a complete url
            if gap:
                _sleep(gap)
        if not failed:
            for pid in batch:
                counts[pid] = len(seen[pid])
    return counts


def check_token(access_token=None, session=None):
    """(ok, message): one cheap real request, so a wrong token is reported BEFORE a long run starts."""
    access_token = access_token or token()
    if not access_token:
        return False, "no Meta API token set"
    if session is None:
        import requests
        session = requests.Session()
    params = {"access_token": access_token, "ad_reached_countries": json.dumps(["FR"]),
              "ad_active_status": "ACTIVE", "ad_type": "ALL", "search_terms": "a", "fields": "id", "limit": 1}
    try:
        data = _get(session, _base(), params, tries=2)
    except ApiAuthError as exc:
        return False, str(exc)
    except RuntimeError as exc:
        return False, str(exc)
    return True, "Meta API token works" if isinstance(data, dict) else "unexpected answer"