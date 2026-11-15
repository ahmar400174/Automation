"""Connect the meta_ads_verifier package to the shared verification cache.

verify_active.verify_targets calls fill_cache when WH_USE_META_PACKAGE=1.
Answers land in the same cache the browser verifier and the reports already
read. A page this package cannot parse is left untouched for the browser.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from typing import Any

SOURCE = "meta_ads_verifier"


def to_cache_entry(active: Any, all_ads: Any = None) -> dict | None:
    """Map package results to one verify_active cache entry, or None.

    A positive count is the embedded Ad Library count. A zero is stored only
    when the active view is empty and the all-ads view also parsed, so an
    unreadable page is never published as 0.
    """
    status = getattr(active, "status", None)
    count = getattr(active, "ad_count", None)
    name = str(getattr(active, "page_name", "") or "")
    http_status = getattr(active, "http_status", None)
    if status == "active" and isinstance(count, int) and count > 0:
        return {
            "state": "live",
            "live": count,
            "error": None,
            "confirmed": True,
            "approximate": False,
            "source": SOURCE,
            "basis": "Meta Ad Library search_results_connection.count",
            "http_status": http_status,
            "page_name": name,
        }
    if status in {"inactive", "deleted"} and count == 0 and all_ads is not None:
        all_status = getattr(all_ads, "status", None)
        all_count = getattr(all_ads, "ad_count", None)
        if all_status not in {"active", "inactive", "deleted", "not_found"}:
            return None
        if not isinstance(all_count, int):
            return None
        if all_status == "not_found" and status != "deleted":
            return None
        all_name = str(getattr(all_ads, "page_name", "") or "")
        return {
            "state": "inactive",
            "live": 0,
            "error": None,
            "confirmed": True,
            "all_ads": all_count,
            "approximate": False,
            "source": SOURCE,
            "basis": "active view is 0 and the all-ads view was parsed",
            "http_status": http_status,
            "page_name": name or all_name,
        }
    return None


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def _proxies() -> list[str]:
    from meta_ads_verifier.proxies import load_proxy_list

    return load_proxy_list(os.environ.get("META_PROXY"), os.environ.get("META_PROXY_FILE"))


async def _fetch(items, active_status: str, concurrency: int, timeout: float, retries: int, impersonate: str):
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    from meta_ads_verifier.engine import ChallengeCache, CurlFetcher, ProxyRotator, Stats, verify_one

    rotator = ProxyRotator(_proxies(), os.environ.get("META_SESSION_MODE", "auto"))
    stats = Stats()
    cache = ChallengeCache()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    found: dict[tuple[str, str], Any] = {}

    async with AsyncSession(
        impersonate=impersonate,
        max_clients=max(1, concurrency),
        timeout=timeout,
        discard_cookies=True,
        curl_options={CurlOpt.FRESH_CONNECT: 1, CurlOpt.FORBID_REUSE: 1},
    ) as session:
        fetcher = CurlFetcher(session, timeout)

        async def one(item):
            page_id, country, _name = item
            async with semaphore:
                result = await verify_one(
                    str(page_id),
                    fetcher=fetcher,
                    rotator=rotator,
                    cache=cache,
                    country=str(country),
                    active_status=active_status,
                    max_retries=retries,
                    stats=stats,
                )
            found[(str(page_id), str(country).upper())] = result

        await asyncio.gather(*(one(item) for item in items))
    return found


def fill_cache(pending, cache: dict, cache_path) -> set[str]:
    """Check pending (page_id, country, name) rows and store trusted answers.

    Returns cache keys written on this call. Unparsed or blocked rows are not
    written, so the browser verifier can still try them.
    """
    rows = []
    seen = set()
    for item in pending or ():
        page_id = str(item[0] or "").strip()
        country = str(item[1] or "").strip().upper()
        name = str(item[2] if len(item) > 2 else "") or page_id
        if not page_id or not country:
            continue
        key = (page_id, country)
        if key in seen:
            continue
        seen.add(key)
        rows.append((page_id, country, name))
    if not rows:
        return set()

    try:
        import curl_cffi  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("curl_cffi is not installed; pip install -r requirements.txt") from exc

    proxies = _proxies()
    concurrency = _int_env("WH_META_CONCURRENCY", 8 if not proxies else 40, 1, 80)
    retries = _int_env("WH_META_RETRIES", 2 if not proxies else 4, 1, 6)
    try:
        timeout = float(os.environ.get("WH_META_TIMEOUT", "20") or 20)
    except ValueError:
        timeout = 20.0
    impersonate = os.environ.get("WH_META_IMPERSONATE", "chrome120") or "chrome120"
    route = "direct" if not proxies else f"{len(proxies)} proxy gateway(s)"
    total = len(rows)
    print(
        f"meta_ads_verifier package linked: checking {total} page(s) "
        f"via {route}, concurrency {concurrency}.",
        flush=True,
    )

    active_map = asyncio.run(_fetch(rows, "active", concurrency, timeout, retries, impersonate))
    zeros = []
    for page_id, country, name in rows:
        result = active_map.get((page_id, country))
        if result is not None and result.status in {"inactive", "deleted"} and result.ad_count == 0:
            zeros.append((page_id, country, name))
    all_map = {}
    if zeros:
        print(f"meta_ads_verifier package: confirming {len(zeros)} empty page(s) on the all-ads view.", flush=True)
        all_map = asyncio.run(_fetch(zeros, "all", concurrency, timeout, retries, impersonate))

    import verify_active

    answered: set[str] = set()
    done = 0
    for page_id, country, name in rows:
        done += 1
        key = verify_active.cache_key(page_id, country)
        entry = to_cache_entry(active_map.get((page_id, country)), all_map.get((page_id, country)))
        if entry is None:
            result = active_map.get((page_id, country))
            sign = getattr(result, "status", None) or "no answer"
            print(f"[{done}/{total}] {country} {name[:34]:34s} -> {sign}", flush=True)
            continue
        entry.update({
            "name": name,
            "page_id": page_id,
            "country": country,
            "attempts": int(getattr(active_map.get((page_id, country)), "attempts", 1) or 1),
            "checked": dt.datetime.now().isoformat(timespec="seconds"),
        })
        cache[key] = entry
        answered.add(key)
        sign = entry["live"]
        print(f"[{done}/{total}] {country} {name[:34]:34s} -> {sign}", flush=True)
        if len(answered) % 25 == 0:
            verify_active.save_cache(cache, cache_path)
    if answered:
        verify_active.save_cache(cache, cache_path)
    return answered
