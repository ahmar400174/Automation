"""CLI for the no-cooldown Meta page verifier."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from tqdm.asyncio import tqdm

from meta_ads_verifier import __version__
from meta_ads_verifier.engine import (
    CurlFetcher,
    ProxyRotator,
    ResultWriter,
    Stats,
    meta_from,
    run_verification,
)
from meta_ads_verifier.parser import classify_target, load_inputs, parse_library_html
from meta_ads_verifier.proxies import SESSION_MODES, inject_session, load_proxy_list


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meta_ads_verifier",
        description=(
            "Verify Facebook pages against the public Ad Library at high concurrency. "
            "HTTP 403/429 drops that proxy and retries immediately. The queue never pauses."
        ),
    )
    parser.add_argument("--input", "-i", help="TXT, CSV, or XLSX. One page id or URL per row.")
    parser.add_argument("--output", "-o", default="verified.xlsx", help="Excel snapshot path.")
    parser.add_argument("--html", default=None, help="HTML snapshot path. Defaults beside the xlsx.")
    parser.add_argument("--proxy", default=os.environ.get("META_PROXY"), help="http://user:pass@host:port")
    parser.add_argument("--proxy-file", default=os.environ.get("META_PROXY_FILE"), help="One proxy URL per line.")
    parser.add_argument("--session-mode", default="auto", choices=SESSION_MODES)
    parser.add_argument("--concurrency", type=int, default=80, help="Semaphore size. Default 80.")
    parser.add_argument("--max-retries", type=int, default=4, help="Immediate retries per page. No sleep between them.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--country", default="ALL", help="Ad Library country filter. ALL or ISO code.")
    parser.add_argument("--active-status", default="active", choices=("active", "inactive", "all"))
    parser.add_argument("--impersonate", default="chrome120", help="curl_cffi TLS fingerprint. Default chrome120.")
    parser.add_argument("--flush-every", type=int, default=50, help="Rewrite xlsx and html every N rows.")
    parser.add_argument("--direct", action="store_true", help="Run without a proxy. One IP will not hold 1000/min.")
    parser.add_argument("--self-test", action="store_true", help="Run offline concurrency, parser, and export checks.")
    parser.add_argument("--version", action="version", version=f"meta_ads_verifier {__version__}")
    return parser


def _html_path(output: str, html: str | None) -> str:
    if html:
        return html
    path = Path(output)
    return str(path.with_suffix(".html"))


async def _metrics(bar, stats: Stats, stop: asyncio.Event) -> None:
    last_n = 0
    last_t = time.perf_counter()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
        now = time.perf_counter()
        done = stats.processed
        dt = max(now - last_t, 1e-6)
        pps = (done - last_n) / dt
        last_n = done
        last_t = now
        rate = (stats.success / done * 100.0) if done else 0.0
        bar.set_postfix_str(
            f"pps={pps:.1f} ppm={pps * 60:.0f} processed={done} success={rate:.1f}% inflight={stats.inflight}",
            refresh=False,
        )
        bar.refresh()


async def _execute(args: argparse.Namespace) -> int:
    if args.concurrency < 1:
        print("concurrency must be >= 1", file=sys.stderr)
        return 2
    if args.max_retries < 1:
        print("max-retries must be >= 1", file=sys.stderr)
        return 2
    proxies = load_proxy_list(args.proxy, args.proxy_file)
    if not proxies and not args.direct:
        print("Pass --proxy, --proxy-file, or META_PROXY. Use --direct only to debug a single IP.", file=sys.stderr)
        return 2
    targets = load_inputs(args.input)
    if not targets:
        print("input has no pages", file=sys.stderr)
        return 2

    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    rotator = ProxyRotator(proxies, args.session_mode)
    stats = Stats()
    html_path = _html_path(args.output, args.html)
    writer = ResultWriter(args.output, html_path, args.flush_every, lambda: meta_from(stats))
    writer_task = asyncio.create_task(writer.run())
    stop = asyncio.Event()
    bar = tqdm(
        total=len(targets),
        unit="page",
        dynamic_ncols=True,
        desc="verify",
        mininterval=0.2,
    )
    metrics_task = asyncio.create_task(_metrics(bar, stats, stop))
    exit_code = 0
    try:
        async with AsyncSession(
            impersonate=args.impersonate,
            max_clients=args.concurrency,
            timeout=args.timeout,
            discard_cookies=True,
            curl_options={
                CurlOpt.FRESH_CONNECT: 1,
                CurlOpt.FORBID_REUSE: 1,
            },
        ) as session:
            fetcher = CurlFetcher(session, args.timeout)
            await run_verification(
                targets,
                fetcher=fetcher,
                rotator=rotator,
                concurrency=args.concurrency,
                max_retries=args.max_retries,
                country=args.country.upper(),
                active_status=args.active_status,
                writer=writer,
                stats=stats,
                progress=bar,
            )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        stop.set()
        metrics_task.cancel()
        try:
            await writer.close()
            await writer_task
        finally:
            bar.close()
            try:
                await metrics_task
            except asyncio.CancelledError:
                pass
    elapsed = max(time.perf_counter() - stats.started, 1e-6)
    print(
        f"processed={stats.processed} success={stats.success_rate():.1f}% "
        f"active={stats.active} inactive={stats.inactive} retries={stats.retries} "
        f"pps={stats.processed / elapsed:.2f} ppm={(stats.processed / elapsed) * 60:.0f} "
        f"dropped_exits={rotator.dropped}",
        file=sys.stderr,
    )
    print(args.output)
    print(html_path)
    return exit_code


class _SilentBar:
    def update(self, n: int = 1) -> None:
        return None

    def set_postfix_str(self, text: str, refresh: bool = False) -> None:
        return None

    def refresh(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ScriptedFetcher:
    """Offline transport: first hit on a page is 429, second is a library document."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.calls: list[tuple[str, str | None]] = []
        self.inflight = 0
        self.max_inflight = 0
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()

    async def request(self, method: str, url: str, proxy: str | None, cookies: dict) -> object:
        async with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.calls.append((method, proxy))
        try:
            await asyncio.sleep(self.delay)
            if method == "POST":
                return _FakeResponse(200, "", {"rd_challenge": "solved"})
            key = url
            async with self._lock:
                first = key not in self._seen
                self._seen.add(key)
            if first:
                return _FakeResponse(429, "rate", {})
            page_id = "15087023444"
            if "view_all_page_id=" in url:
                page_id = url.split("view_all_page_id=", 1)[1].split("&", 1)[0]
            if page_id == "1":
                body = (
                    '"ad_library_main":{"search_results_connection":{"count":0,"edges":[]}},'
                    '"ad_library_page_info":{"page_info":null}'
                )
            else:
                body = (
                    '"ad_library_main":{"search_results_connection":{"count":3,"edges":[]}},'
                    '"ad_library_page_info":{"page_info":{"page_name":"Nike","page_is_deleted":false}}'
                )
            return _FakeResponse(200, body, {})
        finally:
            async with self._lock:
                self.inflight -= 1


class _FakeResponse:
    def __init__(self, status_code: int, text: str, cookies: dict):
        self.status_code = status_code
        self.text = text
        self.url = "https://www.facebook.com/ads/library/"
        self.cookies = cookies


def _self_test() -> int:
    nike = (
        '"ad_library_main":{"search_results_connection":{"count":95,"edges":[]}},'
        '"ad_library_page_info":{"page_info":{"page_name":"Nike","page_is_deleted":false,"profile_photo":"https://cdn/a.jpg"}}'
    )
    missing = (
        '"ad_library_main":{"search_results_connection":{"count":0,"edges":[]}},'
        '"ad_library_page_info":{"page_info":null}'
    )
    parsed = parse_library_html(nike)
    assert parsed and parsed["status"] == "active" and parsed["ad_count"] == 95 and parsed["page_name"] == "Nike"
    missing_parsed = parse_library_html(missing)
    assert missing_parsed and missing_parsed["status"] == "not_found"
    assert parse_library_html("<html>login</html>") is None
    assert classify_target("15087023444") == ("15087023444", "id")
    assert classify_target("page:99") == ("99", "id")
    assert classify_target("https://www.facebook.com/ads/library/?view_all_page_id=55&country=US")[0] == "55"
    assert classify_target("https://www.facebook.com/profile.php?id=77") == ("77", "id")
    assert classify_target("https://www.facebook.com/nike")[1] == "unresolved"

    gateway = "http://brd-customer-abc-zone-residential:secret@brd.superproxy.io:22225"
    first = inject_session(gateway, "auto", "aaa111")
    second = inject_session(gateway, "auto", "bbb222")
    assert first != second
    assert "session-aaa111" in first and "secret" in first
    iproyal = inject_session("http://user:pass@geo.iproyal.com:12321", "auto", "cc33")
    assert "_session-cc33_" in iproyal
    assert inject_session("http://user:pass_{session}@host:1", "none", "z9") == "http://user:pass_z9@host:1"

    async def scenario() -> None:
        fetcher = _ScriptedFetcher(delay=0.01)
        rotator = ProxyRotator([gateway], "brightdata")
        stats = Stats()
        targets = ["15087023444"] * 40 + ["1"] * 10 + ["https://www.facebook.com/nike"] * 10
        with tempfile.TemporaryDirectory() as tmp:
            xlsx = str(Path(tmp) / "out.xlsx")
            html = str(Path(tmp) / "out.html")
            writer = ResultWriter(xlsx, html, flush_every=50, meta_fn=lambda: meta_from(stats))
            task = asyncio.create_task(writer.run())
            started = time.perf_counter()
            await run_verification(
                targets,
                fetcher=fetcher,
                rotator=rotator,
                concurrency=20,
                max_retries=3,
                country="US",
                active_status="active",
                writer=writer,
                stats=stats,
                progress=_SilentBar(),
            )
            elapsed = time.perf_counter() - started
            await writer.close()
            await task
            # 60 network pages each 429 once then succeed, 10 unresolved with no I/O.
            # A global cooldown of even 0.2s per 429 would push this well past 2s.
            if elapsed > 2.0:
                raise AssertionError(f"queue stalled: elapsed {elapsed:.2f}s")
            if fetcher.max_inflight < 8:
                raise AssertionError(f"concurrency not reached: {fetcher.max_inflight}")
            if stats.processed != 60:
                raise AssertionError(f"processed {stats.processed}")
            if stats.success != 50:
                raise AssertionError(f"success {stats.success}")
            if stats.active != 40:
                raise AssertionError(f"active {stats.active}")
            proxies_used = {proxy for _method, proxy in fetcher.calls if proxy}
            if len(proxies_used) < 20:
                raise AssertionError("expected a new proxy session per request")
            if writer.flushes < 2:
                raise AssertionError("expected a mid-run flush and a final flush")
            from openpyxl import load_workbook

            book = load_workbook(xlsx, read_only=True)
            sheet = book.active
            rows = list(sheet.iter_rows(values_only=True))
            book.close()
            if len(rows) != 61:
                raise AssertionError(f"xlsx rows {len(rows)}")
            if not Path(html).read_text(encoding="utf-8").count("<tr>") >= 50:
                raise AssertionError("html snapshot missing rows")

    asyncio.run(scenario())
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.input:
        parser.error("--input is required")
    try:
        return asyncio.run(_execute(args))
    except KeyboardInterrupt:
        print("interrupted; last snapshot was flushed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
