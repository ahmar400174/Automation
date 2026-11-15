"""Async verifier. A 403/429 burns that exit only. The queue never pauses."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from meta_ads_verifier.exporter import write_snapshot
from meta_ads_verifier.parser import (
    challenge_path,
    classify_target,
    library_url,
    parse_library_html,
)
from meta_ads_verifier.proxies import ProxyRotator

_RETRY_STATUS = frozenset({401, 403, 407, 408, 425, 429, 500, 502, 503, 504})
_STOP = object()


@dataclass
class HttpResult:
    status_code: int
    text: str
    url: str
    cookies: dict[str, str]


@dataclass
class Stats:
    processed: int = 0
    success: int = 0
    active: int = 0
    inactive: int = 0
    retries: int = 0
    inflight: int = 0
    started: float = field(default_factory=time.perf_counter)

    def success_rate(self) -> float:
        if self.processed <= 0:
            return 0.0
        return self.success / self.processed * 100.0

    def pages_per_sec(self) -> float:
        elapsed = max(time.perf_counter() - self.started, 1e-6)
        return self.processed / elapsed


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cookies_of(response) -> dict[str, str]:
    found: dict[str, str] = {}
    jar = getattr(response, "cookies", None)
    if jar is not None:
        try:
            for key, value in jar.items():
                found[str(key)] = str(value)
        except Exception:
            found = {}
    if found:
        return found
    headers = getattr(response, "headers", None)
    values: list[str] = []
    if headers is not None and hasattr(headers, "get_list"):
        try:
            values = list(headers.get_list("set-cookie") or [])
        except Exception:
            values = []
    if not values and headers is not None:
        raw = headers.get("set-cookie")
        if raw:
            values = [raw] if isinstance(raw, str) else list(raw)
    for raw in values:
        pair = str(raw).split(";", 1)[0]
        if "=" in pair:
            key, value = pair.split("=", 1)
            found[key.strip()] = value.strip()
    return found


class CurlFetcher:
    """One shared curl_cffi session. Cookies are per request, never pooled."""

    def __init__(self, session, timeout: float):
        self.session = session
        self.timeout = timeout

    async def request(self, method: str, url: str, proxy: str | None, cookies: dict[str, str]) -> HttpResult:
        kwargs = {
            "timeout": self.timeout,
            "discard_cookies": True,
            "allow_redirects": True,
            "referer": "https://www.facebook.com/ads/library/",
            "cookies": cookies or None,
        }
        if proxy:
            kwargs["proxy"] = proxy
        if method == "POST":
            kwargs["headers"] = {"origin": "https://www.facebook.com"}
            response = await self.session.post(url, **kwargs)
        else:
            response = await self.session.get(url, **kwargs)
        return HttpResult(
            status_code=int(response.status_code),
            text=response.text or "",
            url=str(response.url),
            cookies=_cookies_of(response),
        )


class ChallengeCache:
    """Last solved `__rd_verify_` cookie. Workers never wait on each other for it."""

    def __init__(self) -> None:
        self.cookie: str | None = None

    def jar(self) -> dict[str, str]:
        if not self.cookie:
            return {}
        return {"rd_challenge": self.cookie}

    def store(self, cookies: dict[str, str]) -> None:
        value = cookies.get("rd_challenge")
        if value:
            self.cookie = value


@dataclass
class PageResult:
    input: str
    page_id: str
    page_name: str = ""
    status: str = "error"
    runs_ads: str = "unknown"
    ad_count: int | None = None
    page_deleted: str = ""
    http_status: int | None = None
    attempts: int = 0
    proxy: str = ""
    country: str = ""
    checked_at: str = ""
    elapsed_ms: int = 0
    library_url: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "page_id": self.page_id,
            "page_name": self.page_name,
            "status": self.status,
            "runs_ads": self.runs_ads,
            "ad_count": self.ad_count if self.ad_count is not None else "",
            "page_deleted": self.page_deleted,
            "http_status": self.http_status if self.http_status is not None else "",
            "attempts": self.attempts,
            "proxy": self.proxy,
            "country": self.country,
            "checked_at": self.checked_at,
            "elapsed_ms": self.elapsed_ms,
            "library_url": self.library_url,
            "error": self.error,
        }


def _proxy_label(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    from urllib.parse import urlsplit

    parts = urlsplit(proxy)
    if parts.port:
        return f"{parts.hostname}:{parts.port}"
    return parts.hostname or "proxy"


class ResultWriter:
    def __init__(self, xlsx_path: str, html_path: str, flush_every: int, meta_fn):
        self.xlsx_path = xlsx_path
        self.html_path = html_path
        self.flush_every = max(1, flush_every)
        self.meta_fn = meta_fn
        self.queue: asyncio.Queue = asyncio.Queue()
        self.rows: list[dict] = []
        self.flushes = 0

    async def submit(self, row: dict) -> None:
        await self.queue.put(row)

    async def close(self) -> None:
        await self.queue.put(_STOP)

    async def run(self) -> None:
        since_flush = 0
        while True:
            item = await self.queue.get()
            if item is _STOP:
                await asyncio.to_thread(
                    write_snapshot,
                    list(self.rows),
                    self.xlsx_path,
                    self.html_path,
                    self.meta_fn(),
                )
                self.flushes += 1
                return
            self.rows.append(item)
            since_flush += 1
            if since_flush >= self.flush_every:
                since_flush = 0
                await asyncio.to_thread(
                    write_snapshot,
                    list(self.rows),
                    self.xlsx_path,
                    self.html_path,
                    self.meta_fn(),
                )
                self.flushes += 1


def _terminal(status: str) -> bool:
    return status in {"active", "inactive", "not_found", "deleted"}


async def _solve_and_load(fetcher, url: str, proxy: str | None, cookies: dict[str, str], cache: ChallengeCache):
    """GET the library page. Solve the interstitial on this same exit, then GET once more."""
    response = await fetcher.request("GET", url, proxy, cookies)
    cookies = {**cookies, **response.cookies}
    cache.store(cookies)
    path = challenge_path(response.text)
    if path is None:
        return response
    posted = await fetcher.request("POST", path, proxy, cookies)
    cookies = {**cookies, **posted.cookies}
    cache.store(cookies)
    response = await fetcher.request("GET", url, proxy, cookies)
    cache.store(response.cookies)
    return response


async def verify_one(
    raw: str,
    *,
    fetcher,
    rotator: ProxyRotator,
    cache: ChallengeCache,
    country: str,
    active_status: str,
    max_retries: int,
    stats: Stats,
) -> PageResult:
    started = time.perf_counter()
    page_id, kind = classify_target(raw)
    result = PageResult(
        input=raw,
        page_id=page_id if kind == "id" else "",
        country=country,
        checked_at=_now(),
    )
    if kind != "id":
        result.status = "unresolved"
        result.error = (
            "Numeric page id required. Pass the id, a view_all_page_id URL, "
            "or a /pages/.../<id> URL. Logged-out vanity URLs redirect to login."
        )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    url = library_url(page_id, country, active_status)
    result.library_url = url
    last_status = None
    last_error = "no response"
    hard_block = False

    for attempt in range(1, max_retries + 1):
        result.attempts = attempt
        proxy = rotator.next()
        result.proxy = _proxy_label(proxy)
        cookies = cache.jar()
        try:
            response = await _solve_and_load(fetcher, url, proxy, cookies, cache)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            hard_block = False
            rotator.drop(proxy)
            if attempt < max_retries:
                stats.retries += 1
            continue

        last_status = response.status_code
        result.http_status = response.status_code
        parsed = parse_library_html(response.text) if response.status_code == 200 else None
        if parsed is not None:
            result.status = parsed["status"]
            result.runs_ads = parsed["runs_ads"]
            result.ad_count = parsed["ad_count"]
            result.page_name = parsed["page_name"]
            result.page_deleted = parsed["page_deleted"]
            result.error = ""
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        # Real block, challenge that did not clear, or a non-library body.
        # Drop this exit and retry immediately. No sleep. No global flag.
        hard_block = response.status_code in _RETRY_STATUS or challenge_path(response.text) is not None
        last_error = f"HTTP {response.status_code}"
        if response.status_code == 200 and challenge_path(response.text):
            last_error = "challenge not cleared"
        elif response.status_code == 200:
            last_error = "unparsed library response"
        rotator.drop(proxy)
        if attempt < max_retries:
            stats.retries += 1

    result.status = "blocked" if hard_block or (last_status in _RETRY_STATUS) else "error"
    result.runs_ads = "unknown"
    result.error = last_error
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


async def run_verification(
    targets: list[str],
    *,
    fetcher,
    rotator: ProxyRotator,
    concurrency: int,
    max_retries: int,
    country: str,
    active_status: str,
    writer: ResultWriter,
    stats: Stats,
    progress,
) -> None:
    """`concurrency` is the semaphore. One worker's 403 cannot stop the others."""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    cache = ChallengeCache()
    queue: asyncio.Queue = asyncio.Queue()
    for target in targets:
        queue.put_nowait(target)

    async def worker() -> None:
        while True:
            raw = await queue.get()
            if raw is _STOP:
                queue.task_done()
                return
            try:
                async with semaphore:
                    stats.inflight += 1
                    try:
                        result = await verify_one(
                            raw,
                            fetcher=fetcher,
                            rotator=rotator,
                            cache=cache,
                            country=country,
                            active_status=active_status,
                            max_retries=max_retries,
                            stats=stats,
                        )
                    finally:
                        stats.inflight -= 1
                stats.processed += 1
                if _terminal(result.status):
                    stats.success += 1
                if result.status == "active":
                    stats.active += 1
                elif result.status == "inactive":
                    stats.inactive += 1
                progress.update(1)
                await writer.submit(result.as_dict())
            finally:
                queue.task_done()

    worker_count = max(1, concurrency)
    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
    for _ in range(worker_count):
        queue.put_nowait(_STOP)
    await asyncio.gather(*tasks)


def meta_from(stats: Stats) -> dict:
    return {
        "processed": stats.processed,
        "success_rate": stats.success_rate(),
        "pps": stats.pages_per_sec(),
        "inflight": stats.inflight,
        "retries": stats.retries,
        "active": stats.active,
        "inactive": stats.inactive,
    }
