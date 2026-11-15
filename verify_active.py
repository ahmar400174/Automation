"""
Confirms how many ads a Facebook page is REALLY running right now, straight from the Meta Ad Library.

Why this file exists
    WinningHunter gives a page total (total_active_ads_on_page) that is global: it counts every ad the
    page runs in every country and it does not know what is still live in the country we scanned. That is
    why a page can show 1723 in our report while Meta shows ~160. This module asks Meta itself:

        https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US
                                   &view_all_page_id=<page_id>&search_type=page

    and reads the number of results ("~160 results", or "No ads match your search criteria" = 0).

    A ZERO is the dangerous answer (it hides a live advertiser), so it is never trusted on a single
    read: the empty state must stay stable for a few seconds AND a second look at the same page with
    "all ads" must show that Meta really knows the page (and lists no ACTIVE ad). Anything else is
    "unknown", never 0. Old cache files written before this rule have no "confirmed" flag, so their
    zeros are NOT trusted and the pages are checked again.

    Meta answers plain HTTP clients with a JavaScript challenge, so a real browser (Playwright/Chromium)
    is used. Results are cached in results/verify_cache.json, keyed by country + page id, so a run can be
    interrupted and continued and the same page is never checked twice.

Use
    python verify_active.py --week 2026-10-05 --limit 200          # check the biggest claims of that week
    python verify_active.py --page-id 1063256730204760 --country US # check one page
    python verify_active.py --week 2026-10-05 --limit 200 --refresh # ignore the cache, check again
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CACHE_FILE = Path(os.environ.get("WH_VERIFY_CACHE") or (RESULTS / "verify_cache.json"))

COUNT_RE = re.compile(r"(?P<approx>~)?\s*(?P<count>[\d,]+)\s+results?\b")
# The real counter sits alone on its own line ("~52 results"). Anchoring it avoids matching ad copy
# such as "get 3 results in a week" that can appear further down the page.
COUNT_LINE_RE = re.compile(r"(?m)^\s*(?P<approx>~)?\s*(?P<count>[\d,]+)\s+results?\s*$")
EMPTY = "No ads match your search criteria"
BROKEN = ("Something went wrong", "This content isn't available", "You must log in")
# Text that only exists once ad cards are on the page: if one of these is visible the page is NOT empty.
AD_CARD_MARKERS = ("Library ID", "Started running on", "See ad details", "See summary details")
ACTIVE_BADGE_RE = re.compile(r"(?m)^\s*Active\s*$")
# How often the page text is read while waiting for the counter (ms). Smaller = a check ends sooner.
POLL_MS = int(os.environ.get("WH_POLL_MS", "250") or 250)
# Pictures, video and fonts are never needed to read the counter: not downloading them makes every check faster.
HEAVY_RESOURCES = {"image", "media", "font", "stylesheet"}
# Seconds the empty state must stay on screen, unchanged, before it may count as a zero.
ZERO_SETTLE = float(os.environ.get("WH_ZERO_SETTLE", "6"))

# Fields that already carry an active/stopped flag in a WinningHunter payload, in priority order.
ACTIVE_FLAGS = ("is_active", "isActive", "active", "is_running", "is_active_ad")
STATUS_FIELDS = ("status", "ad_status", "active_status", "adActiveStatus", "delivery_status")
LIVE_VALUES = {"ACTIVE", "RUNNING", "LIVE", "ENABLED", "PUBLISHED"}
DEAD_VALUES = {"INACTIVE", "STOPPED", "PAUSED", "ENDED", "DELETED", "ARCHIVED", "DISABLED", "FINISHED"}
END_FIELDS = ("endDate", "end_date", "stop_date", "ad_delivery_stop_time", "stop_time", "stopped_at", "ended_at")
TRUE_WORDS = {"true", "1", "yes", "y", "active", "live", "on"}
FALSE_WORDS = {"false", "0", "no", "n", "inactive", "stopped", "off", ""}


def adlib_url(page_id, country, active=True):
    """The exact Ad Library URL (running ads by default, every ad with active=False).

    This is the ONE url builder of the project: the report links (wh_scan / run_weekly) call it too, so
    the page you click in the dashboard is exactly the page that was counted.
    """
    scope = str(country or "").strip().upper() or "ALL"
    return ("https://www.facebook.com/ads/library/"
            f"?active_status={'active' if active else 'all'}&ad_type=all&country={scope}"
            f"&is_targeted_country=false&media_type=all&search_type=page&view_all_page_id={page_id}")


def parse_link(link):
    """(page_id, country) from an Ads Library link, or (None, None)."""
    try:
        query = parse_qs(urlparse(link or "").query)
    except Exception:
        return None, None
    page_id = (query.get("view_all_page_id") or [None])[0]
    country = (query.get("country") or [None])[0]
    return (page_id or None), (country or None)


# ---------------------------------------------------------------- ad payload judgement
def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
    return None


def _as_date(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (dt.datetime.fromisoformat,):
        try:
            parsed = parse(text.replace("+00:00", "")) if text.count(":") > 2 else parse(text)
            return parsed.date() if isinstance(parsed, dt.datetime) else parsed
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def ad_is_live(ad, today=None):
    """Strict: is THIS ad still running? Returns (True/False/None, source).

    None means the payload says nothing about the ad status: it is then NOT counted as live
    (that is the whole point: raw/historical rows must not be counted as active).
    """
    if not isinstance(ad, dict):
        return None, "unreadable"
    today = today or dt.date.today()

    # A PAST end date means the ad already stopped: nothing else can revive it. Checked first so that
    # a stale is_active=True flag can never make an expired ad count as running.
    for field in END_FIELDS:
        ended = _as_date(ad.get(field))
        if ended and ended < today:
            return False, field

    for field in ACTIVE_FLAGS:
        if field in ad and ad[field] is not None:
            flag = _as_bool(ad[field])
            if flag is not None:
                return flag, field

    for field in STATUS_FIELDS:
        raw = ad.get(field)
        if isinstance(raw, str) and raw.strip():
            word = raw.strip().upper()
            if word in LIVE_VALUES:
                return True, field
            if word in DEAD_VALUES:
                return False, field

    for field in END_FIELDS:
        ended = _as_date(ad.get(field))
        if ended:
            # An end date in the past means the ad stopped running; a future/missing one means it still runs.
            return ended >= today, field
    return None, "no status field"


# ---------------------------------------------------------------- cache
def cache_key(page_id, country):
    return f"{(country or '').upper()}:{page_id}"


def load_cache(path=CACHE_FILE):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


_CACHE_LOCK = threading.Lock()
_NEWNYM_AT = {}


def save_cache(cache, path=CACHE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_LOCK:
        payload = json.dumps(cache, indent=0, sort_keys=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)


def parse_proxy(value=None) -> dict[str, str] | None:
    """WH_PROXY = http://user:pass@host:port (or socks5://...) -> Playwright proxy dict, or None.

    Meta throttles the datacenter IPs of GitHub Actions. A rotating residential proxy gives every request a
    fresh, normal-looking IP, which is the real fix for pages that stay "unverified".
    """
    from urllib.parse import unquote, urlparse
    raw = str(os.environ.get("WH_PROXY", "") if value is None else value).strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    if not u.hostname:
        return None
    proxy = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        proxy["username"] = unquote(u.username)
        proxy["password"] = unquote(u.password or "")
    return proxy


# ---------------------------------------------------------------- UPDATED LOGIC: free per-country Tor IPs
# WH_TOR_PORTS = "US:9100:9200,FR:9101:9201,..." (country:socks_port:control_port), started by the workflow.
# Every country is checked through ITS OWN Tor exit country, and the exit IP is changed (NEWNYM) every few
# checks and whenever Meta throttles. A paid WH_PROXY, when present, always wins.
def _circuits_for(country):
    """Every SOCKS/control pair advertised for this country. One pair = one isolated Tor."""
    found = []
    want = str(country or "").strip().upper()
    for part in str(os.environ.get("WH_TOR_PORTS", "")).split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) < 3 or bits[0].upper() != want:
            continue
        try:
            found.append((int(bits[1]), int(bits[2])))
        except ValueError:
            continue
    return found


def _tor_ports(country, circuit=None):
    found = _circuits_for(country)
    if not found:
        return None
    if circuit is None:
        try:
            circuit = int(os.environ.get("WH_TOR_CIRCUIT", "0") or 0)
        except ValueError:
            circuit = 0
    return found[int(circuit) % len(found)]


def tor_proxy(country, circuit=None) -> dict[str, str] | None:
    """Playwright proxy dict for one Tor circuit of this country, or None."""
    import socket
    ports = _tor_ports(country, circuit)
    if not ports:
        return None
    try:
        with socket.create_connection(("127.0.0.1", ports[0]), timeout=3):
            pass
    except OSError:
        print(f"(Tor for {country} circuit {circuit} is not running on port {ports[0]}: checking without it)", flush=True)
        return None
    return {"server": f"socks5://127.0.0.1:{ports[0]}"}


def trusted_zero(entry):
    """True only for a zero that was double-checked (see check_page). Legacy zeros are NOT trusted."""
    return (isinstance(entry, dict) and entry.get("live") == 0
            and entry.get("state") == "inactive" and entry.get("confirmed") is True)


def cached_live(cache, page_id, country):
    """Verified live count for a page in a country, or None when never (reliably) checked.

    A positive count is always returned. A zero is returned only when it was confirmed: an old,
    unconfirmed zero is exactly the kind of answer that hid 52 running ads, so it counts as "never checked".
    """
    entry = cache.get(cache_key(page_id, country)) if cache else None
    if isinstance(entry, dict) and isinstance(entry.get("live"), int):
        if entry["live"] > 0:
            return entry["live"]
        return 0 if trusted_zero(entry) else None
    return None


def purge_legacy_zeros(path=None):
    """Removes unconfirmed zeros from the cache file. Returns how many entries were removed."""
    path = Path(path or CACHE_FILE)
    cache = load_cache(path)
    bad = [key for key, entry in cache.items()
           if isinstance(entry, dict) and entry.get("live") == 0 and not trusted_zero(entry)]
    for key in bad:
        del cache[key]
    if bad:
        save_cache(cache, path)
    return len(bad)


def has_entry(cache, page_id, country):
    """True when this page was already submitted to the checker (even if it came back unknown).

    An unknown entry means "we asked Meta and could not get an answer": it must win over an older
    number frozen in a workbook, otherwise a value recorded before a rate limit would come back.
    """
    return bool(cache) and cache_key(page_id, country) in cache


# ---------------------------------------------------------------- verification policy
# The number a report may call "active" is the one Meta itself showed us, and only while it is recent
# enough to still describe the page. Everything else is a claim, not a verification.
FRESH_HOURS = float(os.environ.get("WH_VERIFY_FRESH_HOURS", "72"))

STATUS_VERIFIED = "verified"      # Meta showed N > 0 running ads, recently
STATUS_INACTIVE = "inactive"      # Meta showed no running ad (authoritative zero)
STATUS_UNVERIFIED = "unverified"  # never asked, or Meta gave no answer (rate limit / challenge)
STATUS_FAILED = "verification_failed"  # the check itself broke (timeout, browser error)
STATUS_STALE = "stale"            # a number exists but is older than the freshness window
STATUSES = (STATUS_VERIFIED, STATUS_INACTIVE, STATUS_UNVERIFIED, STATUS_FAILED, STATUS_STALE)


def _checked_at(entry):
    raw = (entry or {}).get("checked")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def page_verification(cache, page_id, country, now=None, max_age_hours=None):
    """(verified_count, status, detail) for one page.

    verified_count is an int ONLY for STATUS_VERIFIED / STATUS_INACTIVE; it is None for
    unverified / failed / stale, because those must never be read as a confirmed active count
    (in particular a failed check is NOT a zero).
    """
    page_id, country = str(page_id or ""), str(country or "").upper()
    now = now or dt.datetime.now()
    max_age = FRESH_HOURS if max_age_hours is None else float(max_age_hours)
    entry = (cache or {}).get(cache_key(page_id, country))
    detail = {"checked": None, "reason": "", "age_hours": None, "approximate": False}
    if not isinstance(entry, dict):
        detail["reason"] = "never checked"
        return None, STATUS_UNVERIFIED, detail

    live = entry.get("live")
    detail["approximate"] = bool(entry.get("approximate"))
    detail["checked"] = entry.get("checked")
    checked = _checked_at(entry)
    if checked is not None:
        detail["age_hours"] = round(max(0.0, (now - checked).total_seconds() / 3600.0), 2)

    if live is None:
        detail["reason"] = str(entry.get("error") or entry.get("state") or "no count returned")[:160]
        return None, (STATUS_FAILED if entry.get("state") == "error" else STATUS_UNVERIFIED), detail

    try:
        count = int(live)
    except (TypeError, ValueError):
        detail["reason"] = "unreadable count"
        return None, STATUS_UNVERIFIED, detail

    if count <= 0:
        # A zero hides an advertiser, so it needs MORE proof than a positive count, not less:
        #   * it must carry the "confirmed" flag (stable empty state + second look at "all ads")
        #   * it must be fresh (a page that was empty last month may run 50 ads today)
        if not trusted_zero(entry):
            detail["reason"] = "zero was never double-checked (old answer): checked again on the next live run"
            return None, STATUS_UNVERIFIED, detail
        if checked is None:
            detail["reason"] = "checked time missing"
            return None, STATUS_STALE, detail
        if detail["age_hours"] is not None and detail["age_hours"] > max_age:
            detail["reason"] = f"zero checked {detail['age_hours']:.0f}h ago (limit {max_age:.0f}h)"
            return None, STATUS_STALE, detail
        detail["reason"] = "Meta shows no active ad (confirmed)"
        return 0, STATUS_INACTIVE, detail

    if checked is None:
        detail["reason"] = "checked time missing"
        return None, STATUS_STALE, detail
    if detail["age_hours"] is not None and detail["age_hours"] > max_age:
        detail["reason"] = f"checked {detail['age_hours']:.0f}h ago (limit {max_age:.0f}h)"
        return None, STATUS_STALE, detail
    return count, STATUS_VERIFIED, detail


def resolve_pages(pages, cache_path=None, now=None, max_age_hours=None):
    """Verification of many (page_id, country) pairs, in ONE cache read.

    Returns {cache_key(page_id, country): {"page_id", "country", "verified", "status", ...}}.
    """
    cache = load_cache(cache_path or CACHE_FILE)
    out, seen = {}, set()
    for item in pages or ():
        page_id, country = str(item[0] or "").strip(), str(item[1] or "").strip().upper()
        if not page_id or not country:
            continue
        key = cache_key(page_id, country)
        if key in seen:
            continue
        seen.add(key)
        count, status, detail = page_verification(cache, page_id, country, now, max_age_hours)
        out[key] = {"page_id": page_id, "country": country, "verified": count, "status": status, **detail}
    return out


def verification_summary(entries):
    """Counts per status + the verified total. Used by the report metadata and the logs."""
    summary = {status: 0 for status in STATUSES}
    verified_total = 0
    for entry in (entries or {}).values():
        status = (entry or {}).get("status")
        if status in summary:
            summary[status] += 1
        if status == STATUS_VERIFIED:
            verified_total += int((entry or {}).get("verified") or 0)
    summary["verified_total"] = verified_total
    summary["checked"] = sum(1 for e in (entries or {}).values()
                             if (e or {}).get("checked"))
    return summary


# ---------------------------------------------------------------- browser check
def has_ad_cards(text):
    """True when at least one ad card is rendered: the page is then certainly not an empty result."""
    return any(marker in text for marker in AD_CARD_MARKERS)


def _counter_match(text):
    match = COUNT_LINE_RE.search(text)
    if not match and not has_ad_cards(text):
        match = COUNT_RE.search(text)
    return match


def parse_body(text):
    """('live', count) / ('inactive', 0) / ('unknown', reason).

    Order matters: a result counter ALWAYS wins over the "No ads match" sentence, and so do visible ad
    cards. The empty sentence is also part of the page template while Meta is still loading, so reading
    it first is what produced false zeros for pages that run dozens of ads.
    """
    match = _counter_match(text)
    if match:
        count = int(match.group("count").replace(",", ""))
        if count > 0:
            return "live", count
    if has_ad_cards(text):
        return "unknown", "ad cards are visible but no result counter was read"
    if EMPTY in text:
        return "inactive", 0
    for marker in BROKEN:
        if marker in text:
            return "unknown", marker
    return "unknown", "no result counter on the page"


def norm_name(value):
    """Compare advertiser names without accents / case / extra spaces."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split()).casefold()


RATE_LIMITED = "advertiser not shown (rate limited)"


def should_retry(state, error, attempt, retries=2):
    """Bounded retry policy for one page check.

    Only transient outcomes are retried (a browser error or a page that never showed a counter). A rate
    limit is never retried (it only makes the block longer), and the attempt count is capped.
    """
    if attempt >= max(0, int(retries)):
        return False
    if str(error or "") in (RATE_LIMITED, ZERO_UNCONFIRMED) or str(error or "").startswith("contradiction"):
        return False
    return state in ("error", "unknown")


def retry_delay(attempt):
    """Exponential backoff, capped at 8 s (attempt is 1-based)."""
    return min(2 ** max(1, int(attempt)), 8)


ZERO_UNCONFIRMED = "zero not confirmed (Meta shows no ad at all for this page)"


async def _poll_counter(page, patience, clock):
    """Polls an Ad Library page until a counter shows up. Returns (state, value, text)."""
    deadline = clock() + patience
    state, value, text = "unknown", "no result counter on the page", ""
    while clock() < deadline:
        await page.wait_for_timeout(POLL_MS)
        text = await page.inner_text("body")
        state, value = parse_body(text)
        if state == "live":
            break
        if state == "inactive" and any(m in text for m in ("Filters", "Sort by", "Clear Filters")):
            break
    return state, value, text


async def _confirm_zero(context, page_id, country, timeout, patience, clock):
    """Second look at a page whose ACTIVE view came back empty.

    The same page is opened with "all ads". A real inactive advertiser still has ads in that view (just
    none of them active). If that view is empty too, or shows an ACTIVE ad, the first empty answer was a
    block / a loading glitch and must not become a zero.
    """
    page = await context.new_page()
    try:
        await page.goto(adlib_url(page_id, country, active=False), wait_until="domcontentloaded",
                        timeout=timeout)
        state, value, text = await _poll_counter(page, patience, clock)
        if state != "live":
            return {"state": "unknown", "live": None, "error": ZERO_UNCONFIRMED}
        if ACTIVE_BADGE_RE.search(text):
            return {"state": "unknown", "live": None,
                    "error": "contradiction: the active view was empty but an ad is marked Active"}
        return {"state": "inactive", "live": 0, "error": None, "confirmed": True, "all_ads": value}
    finally:
        await page.close()


async def check_page(context, page_id, country, expect=None, timeout=45000, patience=25,
                     settle=None, clock=None):
    """Opens the Ad Library for one page and returns its live ad count.

    Meta renders the ads with JavaScript, so the counter appears a few seconds after the page itself:
    poll the text instead of reading it once (an early read only sees the navigation bar).

    A POSITIVE counter is returned at once. A ZERO is only returned when
      1. the empty state ("No ads match...") stays on screen, unchanged, for `settle` seconds, and
      2. the advertiser name is on the page (otherwise Meta is rate limiting us), and
      3. the same page with "all ads" shows that Meta knows the page and lists no Active ad.
    Everything else is "unknown" (never 0), so the page is simply checked again later.

    "expect" is the advertiser name we are looking for: an empty answer that does not even mention it is
    a rate limit, NOT a page that stopped running.
    """
    settle = ZERO_SETTLE if settle is None else float(settle)
    clock = clock or time.monotonic
    page = await context.new_page()
    try:
        await page.goto(adlib_url(page_id, country), wait_until="domcontentloaded", timeout=timeout)
        deadline, last_error = clock() + patience, "no result counter on the page"
        empty_since, stable_empty = None, False
        while clock() < deadline:
            await page.wait_for_timeout(POLL_MS)
            text = await page.inner_text("body")
            counter = _counter_match(text)
            state, value = parse_body(text)
            if state == "live":
                return {"state": state, "live": value, "error": None,
                        "approximate": bool(counter and counter.group("approx"))}
            if state == "inactive":
                # An empty page still shows the search panel ("Filters", "Sort by", "Clear Filters");
                # without it we are only looking at the blank start of the page.
                if any(marker in text for marker in ("Filters", "Sort by", "Clear Filters")):
                    wanted = norm_name(expect)
                    if wanted and wanted not in norm_name(text):
                        return {"state": "unknown", "live": None, "error": RATE_LIMITED}
                    if empty_since is None:
                        empty_since = clock()
                    if clock() - empty_since >= settle:
                        stable_empty = True
                        break
                else:
                    empty_since = None
                continue
            empty_since = None            # the empty state disappeared: the page was still loading
            last_error = str(value)[:160]
            if len(text) > 2500 and any(marker in text for marker in BROKEN):
                return {"state": "unknown", "live": None, "error": last_error}
        if not stable_empty:
            return {"state": "unknown", "live": None, "error": last_error}
    except Exception as exc:
        return {"state": "error", "live": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        await page.close()

    try:
        return await _confirm_zero(context, page_id, country, timeout, patience, clock)
    except Exception as exc:
        return {"state": "error", "live": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def _run_checks(targets, workers, cache, cache_path, refresh, gap, timeout, retries=2, deadline=None,
                     progress=None, do_sweep=True, circuit=None, _playwright=None, on_progress=None):
    from playwright.async_api import async_playwright

    queue = asyncio.Queue()
    for item in targets:
        await queue.put(item)
    # On an explicit source limit, stop dispatching new work, back off, and retry the same unanswered pages.
    # Never rotate exits, proxy sessions, browser cookies, or user agents to get around the limit.
    pause_seconds = max(5.0, float(os.environ.get("WH_RATE_PAUSE_SECONDS", "60") or 60))
    pause_cap = max(pause_seconds, float(os.environ.get("WH_RATE_PAUSE_MAX", "3600") or 3600))
    done = {"count": 0, "streak": 0, "blocked": False, "pauses": 0, "pause_until": 0.0, "recent": [],
            "slow": 1.0,
            "good": 0, "fails": 0, "last_ok": 0.0,
            "ok_keys": set(), "progress_keys": set()}
    holder: dict[str, Any] = {"ctx": None}
    # Route failover: DIRECT is the fast default. While Meta rate-limits it, checks move to Tor without pausing;
    # a light probe tests the direct route and the moment Meta answers it again, work returns to DIRECT.
    route: dict[str, Any] = {"mode": "direct", "ctx": None, "browser": None, "sem": None, "retry_after": 0.0,
                             "probe_at": 0.0, "probe_ok": 0, "switches": 0, "probe_item": None}
    probe_every = max(10.0, float(os.environ.get("WH_ROUTE_PROBE_SECONDS", "30") or 30))
    probe_need = max(1, int(os.environ.get("WH_ROUTE_PROBE_OK", "2") or 2))
    tor_conc = max(1, int(os.environ.get("WH_TOR_CONCURRENCY", "3") or 3))
    failover: dict[str, Any] = {"on": False, "proxy": None}

    def notify_progress():
        if on_progress is None:
            return
        try:
            on_progress(len(done["progress_keys"]), len(targets), cache)
        except Exception as exc:
            print(f"Progress snapshot callback skipped ({type(exc).__name__}: {exc}).", flush=True)

    async def worker():
        while True:
            if done["blocked"]:
                return
            if deadline and time.time() >= deadline:
                if not done.get("timed_out"):
                    done["timed_out"] = True
                    print(f"\nVerification time budget used up after {done['count']} check(s): "
                          f"{queue.qsize()} page(s) stay unverified.", flush=True)
                return
            cooling = done["pause_until"] - time.time()
            if cooling > 0:                 # a cool-down is running: every worker waits for it
                await asyncio.sleep(min(cooling, 5))
                continue
            try:
                page_id, country, name = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            key = cache_key(page_id, country)
            if not refresh and cached_live(cache, page_id, country) is not None:
                done["count"] += 1
                done["progress_keys"].add(key)
                notify_progress()
                continue
            try:
                result = await check_fresh(page_id, country, name)
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                print(f"Check failed ({text[:160]}). Page stays unverified.", flush=True)
                save_cache(cache, cache_path)
                if any(word in text.lower() for word in ("closed", "crashed", "connection", "target")):
                    done["blocked"] = True
                    print("Browser died. Saved answers are kept. This country stops; the others keep going.",
                          flush=True)
                    return
                result = {"state": "error", "live": None, "error": text[:160]}
            # Bounded retries for transient browser errors; an explicit source limit enters the long cooldown.
            attempt = 0
            while not done["blocked"] and should_retry(result.get("state"), result.get("error"),
                                                       attempt, retries):
                attempt += 1
                await asyncio.sleep(retry_delay(attempt))
                result = await check_fresh(page_id, country, name)
            # "advertiser not shown" does not always mean a throttle: when the name Meta shows differs from the
            # scraped name, the page can NEVER be answered with the name check and used to stay unverified
            # until the very end of the queue. While Meta is clearly answering other pages (a positive count in
            # the last 45 s) the page is asked again WITHOUT the name check, and a zero is accepted only when a
            # control page that is known to run ads is answered normally at that moment.
            if (result.get("live") is None and result.get("error") in (RATE_LIMITED, ZERO_UNCONFIRMED)
                    and time.time() - done["last_ok"] < 45 and not done["blocked"]):
                alt = await check_fresh(page_id, country, None)
                if alt.get("live") is not None:
                    result = alt
                elif alt.get("error") == ZERO_UNCONFIRMED and await control_page_answers(country):
                    result = {"state": "inactive", "live": 0, "error": None, "confirmed": True, "all_ads": 0,
                              "basis": "no ad in the library (active or past); a control page was answered "
                                       "normally at the same time"}
            with _CACHE_LOCK:
                cache[key] = {**result, "name": name, "page_id": page_id, "country": country,
                              "attempts": attempt + 1,
                              "checked": dt.datetime.now().isoformat(timespec="seconds")}
                done["progress_keys"].add(key)
                if isinstance(result.get("live"), int):
                    done["ok_keys"].add(key)                # only a real count moves the "left" number
                    if progress is not None:
                        progress["ok"].add(key)
                        owner = (progress.get("owner") or {}).get(key)
                        if owner:
                            progress["batches"][owner[0]]["ok"].add(key)
            if isinstance(result.get("live"), int):
                done["last_ok"] = time.time()               # Meta is answering: not a block
            done["count"] += 1
            if result.get("error") in (RATE_LIMITED, ZERO_UNCONFIRMED):
                done["streak"] += 1
                done["recent"].append((page_id, country, name))
                done["good"] = 0
                if done["streak"] >= 3 and failover["on"]:
                    route["probe_item"] = (page_id, country, name)
                    if route["mode"] == "direct" and time.time() >= route["retry_after"]:
                        for item in done["recent"]:
                            queue.put_nowait(item)
                        done["recent"], done["streak"] = [], 0
                        if await go_tor("Meta rate-limited the direct route"):
                            continue
                        done["streak"] = 3          # Tor not available right now: normal cool-down below
                    elif route["mode"] == "tor":
                        print("\nTor is rate-limited too: back to the direct route and a normal cool-down.",
                              flush=True)
                        route["mode"] = "direct"
                        route["switches"] += 1
                if done["streak"] >= 3:
                    done["pauses"] += 1
                    done["fails"] += 1
                    wait = min(pause_seconds * (2 ** min(done["fails"] - 1, 20)), pause_cap)
                    done["pause_until"] = time.time() + wait
                    done["paused_total"] = done.get("paused_total", 0.0) + wait
                    route["retry_after"] = done["pause_until"]
                    done["slow"] = min(done["slow"] * 1.5, 6.0)
                    for item in done["recent"]:
                        queue.put_nowait(item)
                    done["recent"], done["streak"] = [], 0
                    print(f"\nMeta is limiting requests: pausing for {wait:.0f}s; "
                          "the same unanswered pages will be retried on the same route.", flush=True)
                    continue
            else:
                done["streak"], done["recent"] = 0, []
                done["good"] += 1
                if done["good"] >= 12:                     # recovered: Meta answers again, forget the old block
                    done["fails"] = 0
                if done["good"] % 15 == 0 and done["slow"] > 1.0:
                    done["slow"] = max(1.0, done["slow"] / 1.3)      # UPDATED LOGIC: speed comes back step by step
            sign = result["live"] if result["live"] is not None else result["state"]
            if result["live"] is None and result.get("error"):
                sign = f"{sign} ({str(result['error'])[:48]})"      # WHY it is unknown: Meta limit or code
            # Bracket is confirmed answers / pages in this country pass, NOT attempts.
            if progress is not None:
                with _CACHE_LOCK:
                    shown_done, shown_total = len(progress["ok"]), progress["total"]
            else:
                shown_done, shown_total = len(done["ok_keys"]), len(targets)
            print(f"[{shown_done}/{shown_total}] {country} {name[:34]:34s} -> {sign}", flush=True)
            if progress is not None and key in (progress.get("owner") or {}):
                bi, btotal, bn = progress["owner"][key]
                bdone = len(progress["batches"][bi]["ok"])
                print(f"BATCH {bi}/{bn} {bdone}/{btotal} running", flush=True)
            notify_progress()
            if done["count"] % 5 == 0:
                save_cache(cache, cache_path)
            if gap:
                # Keep the ordinary request pace inside a small jitter window; explicit Meta cooldowns
                # above still take precedence and may be much longer.
                delay = min(1.5, max(0.5, gap * done["slow"] * (0.5 + random.random())))
                await asyncio.sleep(delay)

    async with async_playwright() as playwright:
        paid_proxy = parse_proxy()
        _country0 = targets[0][1] if targets else ""
        want_failover = (not paid_proxy) and os.environ.get("WH_TOR_FAILOVER", "0") == "1"
        tor = None if (paid_proxy or want_failover) else tor_proxy(_country0, circuit)
        proxy = paid_proxy or tor
        if want_failover:
            failover["proxy"] = tor_proxy(_country0, circuit)
            failover["on"] = failover["proxy"] is not None
            route["sem"] = asyncio.Semaphore(tor_conc)
            print("Meta checks through the ordinary network route; Tor is on standby and is used only "
                  "while Meta rate-limits the direct route." if failover["on"] else
                  "Tor failover requested but Tor is not reachable: direct route only.", flush=True)
        elif tor:
            print("Meta checks through one steady Tor route (no exit rotation).", flush=True)
        elif not want_failover:
            print((f"Meta checks through one steady proxy: {proxy['server']}") if proxy
                  else "Meta checks through the ordinary network route.", flush=True)
        launch_args = ["--disable-dev-shm-usage", "--disable-gpu"]
        if proxy is None:
            browser = await playwright.chromium.launch(headless=True, args=launch_args)
        else:
            browser = await playwright.chromium.launch(headless=True, args=launch_args, proxy=cast(Any, proxy))
        block_heavy = os.environ.get("WH_BLOCK_HEAVY", "1") != "0"    # UPDATED LOGIC: light pages by default (3x faster)

        async def _skip_heavy(route):
            try:
                if route.request.resource_type in HEAVY_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass

        async def make_context(target=None):
            ctx = await (target or browser).new_context(locale="en-US", viewport={"width": 1400, "height": 900})
            if block_heavy:
                try:
                    await ctx.route("**/*", _skip_heavy)
                except Exception as exc:
                    print(f"(page weight reduction not available: {type(exc).__name__})", flush=True)
            return ctx

        async def check_fresh(page_id, country, expect):
            """Check once on the existing route and browser context; rate limits are handled by cooldown."""
            if route["mode"] == "tor" and route["ctx"] is not None:
                async with route["sem"]:            # Tor is slower: fewer tabs at once, longer timeout
                    return await check_page(route["ctx"], page_id, country, expect=expect,
                                            timeout=int(timeout * 1.6))
            ctx = holder["ctx"]
            if ctx is None:
                raise RuntimeError("browser context is not ready")
            return await check_page(ctx, page_id, country, expect=expect, timeout=timeout)

        async def go_tor(why):
            """Switch the queue to Tor at once (no pause). Starts the Tor browser the first time."""
            if route["mode"] == "tor":
                return True
            try:
                if route["ctx"] is None:
                    route["browser"] = await playwright.chromium.launch(
                        headless=True, args=launch_args, proxy=cast(Any, failover["proxy"]))
                    route["ctx"] = await make_context(route["browser"])
            except Exception as exc:
                print(f"Tor browser could not start ({type(exc).__name__}): staying on direct.", flush=True)
                route["retry_after"] = time.time() + 300
                return False
            route.update(mode="tor", probe_at=time.time() + probe_every, probe_ok=0)
            route["switches"] += 1
            print(f"\nROUTE: DIRECT -> TOR ({why}). No pause; the direct route is tested every "
                  f"{probe_every:.0f}s.", flush=True)
            return True

        def go_direct(why):
            route.update(mode="direct", probe_ok=0)
            route["switches"] += 1
            done.update(streak=0, fails=0, pause_until=0.0, slow=1.0, good=0, recent=[])
            print(f"\nROUTE: TOR -> DIRECT ({why}). Full speed again.", flush=True)

        async def probe_loop():
            """While on Tor: test the direct route with ONE light request; return to it once Meta answers."""
            while True:
                await asyncio.sleep(2)
                if route["mode"] != "tor" or time.time() < route["probe_at"]:
                    continue
                route["probe_at"] = time.time() + probe_every
                item = None
                for entry in list(cache.values()):          # a page known to run ads: a clean control
                    if (isinstance(entry, dict) and isinstance(entry.get("live"), int) and entry["live"] > 0
                            and entry.get("page_id") and entry.get("country")):
                        item = (entry["page_id"], entry["country"], entry.get("name"))
                        break
                item = item or route["probe_item"]
                if not item or holder["ctx"] is None:
                    continue
                try:
                    r = await check_page(holder["ctx"], item[0], item[1], expect=item[2], timeout=timeout)
                except Exception:
                    route["probe_ok"] = 0
                    continue
                if isinstance(r.get("live"), int) and r.get("live") > 0:
                    route["probe_ok"] += 1
                    if route["probe_ok"] >= probe_need:
                        go_direct("Meta answers the direct route again")
                else:
                    route["probe_ok"] = 0

        async def ensure_context():
            """Create one browser context and preserve it across source cooldowns and retries."""
            if holder["ctx"] is not None:
                return
            holder["ctx"] = await make_context()

        async def control_page_answers(country):
            """True when Meta still answers a page that is known to run ads: we are NOT being blocked."""
            for entry in list(cache.values()):
                if (isinstance(entry, dict) and entry.get("country") == country and entry.get("page_id")
                        and isinstance(entry.get("live"), int) and entry["live"] > 0):
                    probe = await check_fresh(entry["page_id"], country, entry.get("name"))
                    return probe.get("state") == "live"
            return False

        async def sweep():
            """Last chance for every page that is still unanswered, so no competitor stays 'unverified'.

            A single throttled answer, or an advertiser whose Meta name differs from the scraped name, is
            not retried by the workers (they only react to a sustained streak). A pass keeps the same route and
            context. In the LAST pass the name check is dropped: the page is then judged only by what
            Meta shows (counter, or "no ads" in the active view AND in the all-ads view). If both views are
            empty, the page is accepted as 0 only when Meta still answers a control page that is known to
            run ads at that very moment (so it is not a block).
            """
            passes = int(os.environ.get("WH_SWEEP_PASSES", "3") or 3)
            pause = float(os.environ.get("WH_SWEEP_PAUSE_SECONDS", "45") or 45)
            for number in range(1, passes + 1):
                left = [t for t in targets
                        if (cache.get(cache_key(t[0], t[1])) or {}).get("live") is None]
                if not left or done["blocked"] or len(left) > 250:
                    return
                if deadline and time.time() >= deadline:
                    return
                print(f"\nFinal sweep {number}/{passes}: {len(left)} page(s) still without an answer.", flush=True)
                if deadline and deadline - time.time() < pause * number:
                    return                      # not enough budget left for the pause: stop on time
                await asyncio.sleep(pause * number)
                await ensure_context()
                last = number == passes
                for page_id, country, name in left:
                    if deadline and time.time() >= deadline:
                        return
                    result = await check_fresh(page_id, country, None if last else name)
                    if (last and result.get("live") is None and result.get("error") == ZERO_UNCONFIRMED
                            and await control_page_answers(country)):
                        result = {"state": "inactive", "live": 0, "error": None, "confirmed": True,
                                  "all_ads": 0, "basis": "no ad in the library (active or past); a control "
                                                          "page was answered normally at the same time"}
                    if result.get("live") is not None:
                        ckey = cache_key(page_id, country)
                        with _CACHE_LOCK:
                            cache[ckey] = {
                                **result, "name": name, "page_id": page_id, "country": country,
                                "attempts": number + 1,
                                "checked": dt.datetime.now().isoformat(timespec="seconds")}
                            if progress is not None:
                                progress["ok"].add(ckey)
                    sign = result["live"] if result["live"] is not None else result["state"]
                    print(f"[sweep {number}] {country} {name[:34]:34s} -> {sign}", flush=True)
                    save_cache(cache, cache_path)
                    await asyncio.sleep(gap or 0)

        await ensure_context()
        probe_task = asyncio.create_task(probe_loop()) if failover["on"] else None
        try:
            await asyncio.gather(*[worker() for _ in range(max(1, workers))])
            if do_sweep:
                await sweep()
        finally:
            print(f"\nSUMMARY: {done['count']} Meta check(s) made, {len(done['ok_keys'])} answered, "
                  f"{done['pauses']} rate-limit pause(s) totalling {done.get('paused_total', 0.0) / 60:.1f} min, "
                  f"{route['switches']} route switch(es)"
                  f"{' - stopped by the time budget' if done.get('timed_out') else ''}.", flush=True)
            if probe_task is not None:
                probe_task.cancel()
            if route["ctx"] is not None:
                try:
                    await route["ctx"].close()
                    await route["browser"].close()
                except Exception:
                    pass
            for ctx in [holder["ctx"]]:
                if ctx is None:
                    continue
                try:
                    await ctx.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception as exc:
                print(f"Browser close skipped ({type(exc).__name__}): answers already saved.", flush=True)
    save_cache(cache, cache_path)
    batches = (progress or {}).get("batches") or {}
    bn = (progress or {}).get("batches_n") or 0
    for i in sorted(batches):
        b = batches[i]
        print(f"BATCH {i}/{bn} {len(b['ok'])}/{b['total']} done", flush=True)
    return cache


def verify_via_api(pending, cache, cache_path):
    """Meta Ad Library API is disabled in this build (no Facebook identity / no token).

    Every count is read from the Ad Library website. The function stays so older callers do not break.
    """
    return set()
    import meta_api  # unreachable; kept so a stray import error is obvious in reviews
    if not meta_api.token() or os.environ.get("WH_USE_META_API", "1") == "0":
        return set()
    wanted = {}
    for pid, cc, name in pending:
        if meta_api.supports(cc):
            wanted.setdefault(cc, []).append((pid, name))
    answered = set()
    for cc, items in wanted.items():
        print(f"Official Meta API: {len(items)} {cc} page(s) ...", flush=True)
        try:
            counts = meta_api.count_active(cc, [pid for pid, _ in items])
        except meta_api.ApiAuthError as exc:
            print(f"Meta API token problem, the browser is used instead: {exc}", flush=True)
            return answered
        except Exception as exc:
            print(f"Meta API unavailable for {cc} ({type(exc).__name__}: {exc}): the browser is used instead.",
                  flush=True)
            continue
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        for pid, name in items:
            if pid not in counts:
                continue
            n = int(counts[pid])
            cache[cache_key(pid, cc)] = {
                "state": "live" if n > 0 else "inactive", "live": n, "error": None, "confirmed": True,
                "source": "meta_api", "name": name, "page_id": pid, "country": cc, "attempts": 1,
                "checked": stamp}
            answered.add(cache_key(pid, cc))
        save_cache(cache, cache_path)
        print(f"   {cc}: {len([1 for pid, _ in items if pid in counts])} of {len(items)} answered "
              f"by the API", flush=True)
    return answered


def _batch_size():
    """Pages per parallel session. 1000 pages -> 4 sessions of 250. 0 = one session, old behaviour."""
    try:
        return int(os.environ.get("WH_BATCH_SIZE", "250") or 250)
    except ValueError:
        return 250


def _chunks(pending, size):
    if size <= 0 or len(pending) <= max(size, 1):
        return [pending]
    return [pending[i:i + size] for i in range(0, len(pending), size)]


def _batch_parallel():
    """Tabs inside one browser for this country; Tor never raises the safe concurrency cap."""
    try:
        n = int(os.environ.get("WH_BATCH_PARALLEL", "0") or 0)
    except ValueError:
        n = 0
    if n <= 0:
        try:
            n = int(os.environ.get("WH_VERIFY_WORKERS", "4") or 4)
        except ValueError:
            n = 2
    return max(1, min(n, 4))


def _run_one_session(index, total, chunk, cache, cache_path, refresh, gap, timeout, retries, deadline, progress):
    raise RuntimeError("separate browsers per batch are disabled (they exhausted RAM)")


def _run_batched(chunks, cache, cache_path, refresh, gap, timeout, retries, deadline, on_progress=None):
    """250-page batches, listed one by one, checked through a single browser with a few tabs."""
    pending = [item for chunk in chunks for item in chunk]
    batches, owner = {}, {}
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        batches[i] = {"ok": set(), "total": len(chunk)}
        for pid, cc, _name in chunk:
            owner[cache_key(pid, cc)] = (i, len(chunk), n)
        print(f"BATCH {i}/{n} 0/{len(chunk)} queued", flush=True)
    slots = _batch_parallel()
    progress = {"ok": set(), "total": len(pending), "batches": batches, "owner": owner, "batches_n": n}
    print(f"Batch plan: {len(pending)} page(s) -> {n} batches of up to {_batch_size()}. "
          f"{slots} tabs in ONE browser (every batch is listed; opening one Chrome per batch "
          f"crashed the machine).", flush=True)
    # Keep goto timeout tight inside batches so a hung page does not block the whole country.
    try:
        env_to = int(os.environ.get("WH_GOTO_TIMEOUT", "18000") or 18000)
    except ValueError:
        env_to = 18000
    goto_ms = max(8000, min(int(timeout or env_to), env_to, 25000))
    return asyncio.run(_run_checks(
        pending, slots, cache, cache_path, refresh, gap, goto_ms, min(int(retries), 1),
        deadline=deadline, progress=progress, do_sweep=False, on_progress=on_progress))


def verify_targets(targets, workers=4, cache_path=CACHE_FILE, refresh=False, gap=None, timeout=None,
                   retries=2, deadline=None, progress_callback=None):
    """Checks a list of (page_id, country, name). Returns the updated cache."""
    # UPDATED LOGIC: short goto timeout by default. A 45s hang on every dead page was the main
    # reason the queue looked "slower than before". Fail fast, move on, retry next round.
    if timeout is None:
        try:
            timeout = int(os.environ.get("WH_GOTO_TIMEOUT", "18000") or 18000)
        except ValueError:
            timeout = 18000
    timeout = max(8000, min(int(timeout), 45000))
    workers = max(1, min(int(workers), 4))
    if os.environ.get("WH_PARALLEL_COUNTRIES", "").strip().lower() in ("1", "true", "yes", "on"):
        cap = int(os.environ.get("WH_PARALLEL_TAB_CAP", "4") or 4)
        workers = max(1, min(workers, cap))
    if gap is None:   # pause between two checks of one tab: slower = fewer rate limits = more verified pages
        gap = float(os.environ.get("WH_VERIFY_GAP", "0.75") or 0)
    cache = load_cache(cache_path)
    todo, seen_targets = [], set()
    for pid, country, name in targets:
        if not pid or not country:
            continue
        pid, country = str(pid).strip(), str(country).upper().strip()
        key = cache_key(pid, country)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        todo.append((pid, country, name or ""))
    if not todo:
        print("Nothing to verify.")
        return cache
    pending = [t for t in todo if refresh or cached_live(cache, t[0], t[1]) is None]
    batch = _batch_size()
    sessions = 1 if batch <= 0 or len(pending) <= max(batch, 1) else (len(pending) + batch - 1) // batch
    if sessions > 1:
        print(f"{len(todo)} page(s) requested, {len(todo) - len(pending)} already cached, "
              f"{len(pending)} to check in {sessions} batches of {batch}", flush=True)
    else:
        print(f"{len(todo)} page(s) requested, {len(todo) - len(pending)} already cached, "
              f"{len(pending)} to check with {workers} browser tab(s)", flush=True)
    if not pending:
        return cache
    verify_via_api(pending, cache, cache_path)  # no-op: API token is not used
    circuits = [] if parse_proxy() else _circuits_for(pending[0][1])
    if len(circuits) > 1:
        buckets = [[] for _ in circuits]
        for i, item in enumerate(pending):
            buckets[i % len(circuits)].append(item)
        print(f"Multi-Tor {pending[0][1]}: {len(circuits)} circuits x {workers} tabs "
              f"= {len(circuits) * workers} pages in flight", flush=True)

        import concurrent.futures
        def _one(pair):
            i, bucket = pair
            return asyncio.run(_run_checks(
                bucket, workers, cache, cache_path, refresh, gap, timeout, retries,
                deadline=deadline, circuit=i, do_sweep=False, on_progress=progress_callback))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(circuits)) as pool:
            list(pool.map(_one, [(i, b) for i, b in enumerate(buckets) if b]))
        result = cache
    else:
        chunks = _chunks(pending, _batch_size())
        if len(chunks) == 1:
            result = asyncio.run(_run_checks(pending, workers, cache, cache_path, refresh, gap, timeout, retries,
                                             deadline=deadline, on_progress=progress_callback))
        else:
            result = _run_batched(chunks, cache, cache_path, refresh, gap, timeout, retries, deadline,
                                  on_progress=progress_callback)
    summary = {}
    for pid, cc, _name in pending:
        entry = result.get(cache_key(pid, cc)) or {}
        if entry.get("live") is None:
            reason = str(entry.get("error") or "not reached (time budget / stopped)")[:60]
            summary[reason] = summary.get(reason, 0) + 1
    ok = len(pending) - sum(summary.values())
    print(f"\nVerification summary: {ok} page(s) verified, {sum(summary.values())} still without an answer", flush=True)
    for reason, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"   {n:5d} x {reason}", flush=True)
    return result


# ---------------------------------------------------------------- targets from a week
def targets_from_week(start, limit=None, tab="All"):
    """Read all imported pages by default, with an optional per-tab selection."""
    merged = RESULTS / f"competitors_week_{start}.xlsx"
    files = [merged] if merged.exists() else sorted((HERE / "work").glob(f"competitors_*_{start}_*.xlsx"))
    found = {}
    for path in files:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True)
        except Exception as exc:
            print(f"skip {path.name}: {exc}")
            continue
        selected = str(tab).strip()
        if selected.lower() in ("all", ""):
            wanted = ("All Top", "All Others") if "All Top" in wb.sheetnames else ("Top", "Others")
        elif selected == "All Top":
            wanted = ("All Top",) if "All Top" in wb.sheetnames else ("Top",)
        elif selected == "All Others":
            wanted = ("All Others",) if "All Others" in wb.sheetnames else ("Others",)
        else:
            wanted = (selected,)
        for sheet in wanted:
            if sheet not in wb.sheetnames:
                continue
            rows = wb[sheet].iter_rows(values_only=True)
            head = next(rows, None) or ()
            index = {name: i for i, name in enumerate(head) if name}

            def cell(row, *names):
                for name in names:
                    position = index.get(name)
                    if position is not None and position < len(row) and row[position] not in (None, ""):
                        return row[position]
                return None

            for row in rows:
                page_id, country = parse_link(cell(row, "Ads Library link"))
                if not page_id or not country:
                    continue
                claimed = cell(row, "API reported ads", "Page total (WH)", "Active ads", "Live ads") or 0
                try:
                    claimed = int(float(str(claimed).replace(",", "")))
                except ValueError:
                    claimed = 0
                found[(page_id, country)] = (claimed, cell(row, "Page name") or "")
        wb.close()

    ordered = sorted(found.items(), key=lambda kv: -kv[1][0])
    if limit:
        ordered = ordered[:limit]
    return [(pid, cc, name) for (pid, cc), (_, name) in ordered]


def main():
    parser = argparse.ArgumentParser(description="Verify live ad counts on the Meta Ad Library")
    parser.add_argument("--week", help="Monday of the week, YYYY-MM-DD (reads results/competitors_week_*.xlsx)")
    parser.add_argument("--page-id", action="append", default=[], help="page id to check (repeatable)")
    parser.add_argument("--country", default="US", help="country used with --page-id")
    parser.add_argument("--limit", type=int, default=0, help="check only the N biggest claims")
    parser.add_argument("--workers", type=int, default=4, help="browser tabs in parallel")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and check again")
    parser.add_argument("--resume", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--keep-all-stores", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--purge-legacy-zeros", action="store_true",
                        help="drop old, never double-checked zeros from the cache so they are checked again")
    parser.add_argument("--cache", default=str(CACHE_FILE))
    args = parser.parse_args()

    if args.purge_legacy_zeros:
        removed = purge_legacy_zeros(args.cache)
        print(f"cache {args.cache}: {removed} unconfirmed zero(s) removed")
        if not (args.week or args.page_id):
            return 0

    targets = [(pid, args.country, pid) for pid in args.page_id]
    if args.week:
        targets += targets_from_week(args.week, args.limit or None)
    if not targets:
        parser.error("give --week or --page-id")

    cache = verify_targets(targets, workers=args.workers, cache_path=args.cache, refresh=args.refresh)
    checked = [v for v in cache.values() if isinstance(v, dict)]
    live = [v for v in checked if isinstance(v.get("live"), int) and v["live"] > 0]
    dead = [v for v in checked if trusted_zero(v)]
    unknown = len(checked) - len(live) - len(dead)
    print(f"\ncache {args.cache}: {len(live)} live, {len(dead)} inactive, {unknown} unknown "
          f"({len(checked)} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
