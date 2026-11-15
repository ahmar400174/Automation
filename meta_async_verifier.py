#!/usr/bin/env python3
"""Async Meta Ad Library verifier with CSV-column preservation and incremental exports.

Safety / operating notes:
- This tool does not rotate Tor circuits or proxy identities to get around Meta 403/429 responses.
- A 403, 429, or explicit rate-limit response stops dispatching new requests for this run; unfinished
  rows are recorded as DEFERRED_RATE_LIMIT. Do not treat that result as zero ads.
- Concurrency is deliberately capped at 6 and requests are globally paced (1.5 seconds by default).
  No throughput guarantee is made; Meta may rate-limit or serve JavaScript/challenge pages.
- aiohttp does not execute JavaScript. Rows without a parseable count are marked UNPARSEABLE, not zero.

Install:
    python -m pip install aiohttp pandas openpyxl tqdm

Input:
    CSV with a page ID column (page_id or Page ID). Country is optional (defaults to ALL).
    All input columns are preserved. Verification Status, Verification Time, Verification Error,
    Verification HTTP Status, and Verified active ads (if absent in the input) are appended.

Run:
    python meta_async_verifier.py --input pages.csv --output-dir results
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm

COUNT_RE = re.compile(r"(?<![\w])(?P<approx>~)?\s*(?P<count>[\d,]+)\s+results?\b", re.I)
SCI_ID_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?[eE][+-]?\d+$")
RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limit",
    "rate-limited",
    "we limit how often",
    "temporarily blocked",
)
USER_AGENT = (
    "Mozilla/5.0 (compatible; AdLibraryVerifier/1.0; "
    "+https://www.facebook.com/ads/library/)"
)


@dataclass(frozen=True)
class PageJob:
    row_index: int
    page_id: str
    country: str
    name: str


class RequestPacer:
    """Enforce a process-wide minimum delay between request starts."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = max(1.0, float(minimum_interval))
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last_start + self.minimum_interval - now
            if delay > 0:
                await asyncio.sleep(delay + random.uniform(0.0, 0.25))
            self._last_start = time.monotonic()


def normalize_header(value: Any) -> str:
    return re.sub(r"[_\s]+", " ", str(value or "").strip().casefold())


def find_column(columns: list[Any], aliases: tuple[str, ...]) -> Any | None:
    normalized_aliases = {normalize_header(alias) for alias in aliases}
    for column in columns:
        if normalize_header(column) in normalized_aliases:
            return column
    return None


def ensure_column(frame: pd.DataFrame, name: str, default: Any = "") -> Any:
    existing = find_column(list(frame.columns), (name,))
    if existing is not None:
        return existing
    frame[name] = default
    return name


def normalize_page_id(value: Any) -> str:
    """Keep page IDs exact when possible, including IDs parsed from scientific notation."""
    text = str(value or "").strip()
    if not text or text.casefold() in {"nan", "none", "null", "<na>"}:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    if SCI_ID_RE.fullmatch(text):
        try:
            number = Decimal(text)
            return format(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f")
        except (InvalidOperation, ValueError):
            return text
    return text


def ad_library_url(page_id: str, country: str) -> str:
    params = urlencode({
        "active_status": "active",
        "ad_type": "all",
        "country": country.upper() or "ALL",
        "is_targeted_country": "false",
        "media_type": "all",
        "search_type": "page",
        "view_all_page_id": page_id,
    })
    return f"https://www.facebook.com/ads/library/?{params}"


def parse_count(body: str) -> tuple[int | None, bool]:
    """Return (count, approximate); an unparseable body is never interpreted as zero."""
    match = COUNT_RE.search(body or "")
    if not match:
        return None, False
    try:
        return int(match.group("count").replace(",", "")), bool(match.group("approx"))
    except (TypeError, ValueError):
        return None, False


def contains_rate_limit(body: str) -> bool:
    normalized = (body or "").casefold()
    return any(marker in normalized for marker in RATE_LIMIT_MARKERS)


def load_input_csv(path: Path) -> tuple[pd.DataFrame, Any, Any | None, Any | None]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "page_id,country,name,site link,Score,Verified active ads\n",
            encoding="utf-8-sig",
        )
        raise FileNotFoundError(
            f"Input CSV was missing, so a starter template was created at: {path}\n"
            "Add one row per page, for example: 123456789,ES,Brand name,,,,\n"
            "Save the CSV and rerun. Relative paths are resolved from the current folder."
        )

    # dtype=str and keep_default_na=False preserve long page IDs and all existing cell values.
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if frame.empty and len(frame.columns) == 0:
        raise ValueError("The input CSV is empty; include a page_id column and at least one data row.")
    page_col = find_column(list(frame.columns), ("page_id", "Page ID", "Facebook Page ID", "view_all_page_id"))
    if page_col is None:
        raise ValueError(
            "No page ID column found. Expected a header named page_id, Page ID, Facebook Page ID, "
            "or view_all_page_id. Existing columns were not modified."
        )
    country_col = find_column(list(frame.columns), ("country", "Country", "country code", "Country Code"))
    name_col = find_column(list(frame.columns), ("name", "Page name", "Page Name", "Advertiser name"))
    return frame, page_col, country_col, name_col


def make_jobs(frame: pd.DataFrame, page_col: Any, country_col: Any | None,
              name_col: Any | None) -> list[PageJob]:
    jobs: list[PageJob] = []
    for index in range(len(frame)):
        page_id = normalize_page_id(frame.at[index, page_col])
        country_value = frame.at[index, country_col] if country_col is not None else "ALL"
        country = str(country_value or "ALL").strip().upper() or "ALL"
        name_value = frame.at[index, name_col] if name_col is not None else ""
        name = str(name_value or "").strip()
        jobs.append(PageJob(index, page_id, country, name))
    return jobs


def result_fields(job: PageJob, status: str, *, count: int | None = None,
                  approximate: bool = False, error: str = "", http_status: int | None = None) -> dict[str, Any]:
    return {
        "row_index": job.row_index,
        "status": status,
        "count": count,
        "approximate": approximate,
        "error": error,
        "http_status": http_status,
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def deferred_result(job: PageJob, attempts: int = 0) -> dict[str, Any]:
    return result_fields(
        job, "DEFERRED_RATE_LIMIT",
        error=f"Not requested after a rate-limit signal; attempts before deferral: {attempts}.",
    )


async def fetch_one(
    job: PageJob,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    pacer: RequestPacer,
    stop_dispatch: asyncio.Event,
    max_retries: int,
) -> dict[str, Any]:
    """Verify a page with bounded transient retries; never retries 403/429."""
    if not job.page_id:
        return result_fields(job, "INVALID_ID", error="The page ID cell is blank or invalid.")

    url = ad_library_url(job.page_id, job.country)
    last_error = ""
    for attempt in range(1, max_retries + 1):
        if stop_dispatch.is_set():
            return deferred_result(job, attempt - 1)

        delay_before_retry: float | None = None
        try:
            async with semaphore:
                if stop_dispatch.is_set():
                    return deferred_result(job, attempt - 1)
                await pacer.wait_turn()
                # A different request can detect a limit while this coroutine waits for pacing.
                if stop_dispatch.is_set():
                    return deferred_result(job, attempt - 1)

                async with session.get(url) as response:
                    body = await response.text(errors="replace")
                    status = response.status
                    if status in (403, 429) or contains_rate_limit(body):
                        stop_dispatch.set()
                        label = f"HTTP {status}" if status in (403, 429) else "rate-limit text in response"
                        return result_fields(
                            job, "RATE_LIMITED_STOP", error=f"{label}; remaining pages deferred this run.",
                            http_status=status,
                        )
                    if status >= 500:
                        last_error = f"HTTP {status} server error"
                        if attempt < max_retries:
                            delay_before_retry = min(30.0, 2.0 ** (attempt - 1))
                        else:
                            return result_fields(job, "FAILED_MAX_RETRIES", error=last_error,
                                                 http_status=status)
                    elif status >= 400:
                        return result_fields(job, f"HTTP_ERROR_{status}", error=f"HTTP {status}",
                                             http_status=status)
                    else:
                        count, approximate = parse_count(body)
                        if count is None:
                            return result_fields(
                                job, "UNPARSEABLE",
                                error="No result counter found; the response may require JavaScript or be a challenge page.",
                                http_status=status,
                            )
                        return result_fields(job, "VERIFIED", count=count, approximate=approximate,
                                             http_status=status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            if attempt >= max_retries:
                return result_fields(job, "FAILED_MAX_RETRIES", error=last_error)
            delay_before_retry = min(30.0, 2.0 ** (attempt - 1))

        # Only transient network/5xx failures reach this bounded retry delay.
        if delay_before_retry is not None:
            await asyncio.sleep(delay_before_retry)

    return result_fields(job, "FAILED_MAX_RETRIES", error=last_error or "Retry cap reached.")


def apply_result(frame: pd.DataFrame, result: dict[str, Any], status_col: Any,
                 time_col: Any, error_col: Any, http_col: Any, count_col: Any) -> None:
    row_index = result["row_index"]
    frame.at[row_index, status_col] = result["status"]
    frame.at[row_index, time_col] = result["checked_at"]
    frame.at[row_index, error_col] = result["error"]
    frame.at[row_index, http_col] = "" if result["http_status"] is None else str(result["http_status"])
    if result["count"] is not None:
        count = result["count"]
        frame.at[row_index, count_col] = f"~{count}" if result["approximate"] else str(count)


def write_exports(frame: pd.DataFrame, output_dir: Path) -> None:
    """Pandas and filesystem work; invoked with asyncio.to_thread."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "meta_results.html"
    html_tmp = output_dir / "meta_results.html.tmp"
    xlsx_path = output_dir / "meta_results.xlsx"
    xlsx_tmp = output_dir / "meta_results.tmp.xlsx"

    frame.to_html(html_tmp, index=False, escape=True, border=0, classes="meta-results")
    os.replace(html_tmp, html_path)
    frame.to_excel(xlsx_tmp, index=False, engine="openpyxl")
    os.replace(xlsx_tmp, xlsx_path)


async def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    frame, page_col, country_col, name_col = load_input_csv(input_path)
    jobs = make_jobs(frame, page_col, country_col, name_col)
    if not jobs:
        raise ValueError("The CSV has a header but no page rows to verify.")

    # Keep the concurrency and request cadence conservative; do not rotate identity after 403/429.
    concurrency = min(max(1, args.concurrency), 6)
    semaphore = asyncio.Semaphore(concurrency)
    pacer = RequestPacer(args.min_interval)
    stop_dispatch = asyncio.Event()
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    # Add result columns only when absent; all original input columns and values remain intact.
    status_col = ensure_column(frame, "Verification Status")
    time_col = ensure_column(frame, "Verification Time")
    error_col = ensure_column(frame, "Verification Error")
    http_col = ensure_column(frame, "Verification HTTP Status")
    count_col = find_column(list(frame.columns), ("Verified active ads", "Verified Active Ads"))
    if count_col is None:
        count_col = ensure_column(frame, "Verified active ads")

    total = len(jobs)
    completed_count = 0
    exported_count = 0
    started = time.monotonic()

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        tasks = [asyncio.create_task(fetch_one(job, session, semaphore, pacer, stop_dispatch,
                                               args.max_retries)) for job in jobs]
        with tqdm(total=total, unit="page", dynamic_ncols=True, desc="Meta checks") as progress:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                apply_result(frame, result, status_col, time_col, error_col, http_col, count_col)
                completed_count += 1
                progress.update(1)

                elapsed = max(0.001, time.monotonic() - started)
                speed = completed_count / elapsed
                eta = (total - completed_count) / speed if speed > 0 else 0.0
                progress.set_postfix_str(
                    f"total={total} processed={completed_count} elapsed={elapsed:.1f}s "
                    f"ETA={eta:.1f}s speed={speed:.2f} pages/s",
                    refresh=False,
                )

                if completed_count - exported_count >= args.export_every:
                    # Copy the frame on the event loop; pandas serialization and disk I/O run in a thread.
                    snapshot = frame.copy(deep=True)
                    await asyncio.to_thread(write_exports, snapshot, output_dir)
                    exported_count = completed_count

    # Always save the final state, including any rows deferred after a limit signal.
    await asyncio.to_thread(write_exports, frame.copy(deep=True), output_dir)

    counts = frame[status_col].value_counts(dropna=False).to_dict()
    elapsed = max(0.001, time.monotonic() - started)
    print(f"\nFinished: {completed_count}/{total} rows in {elapsed:.1f}s "
          f"({completed_count / elapsed:.2f} rows/sec).")
    print("Statuses: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"Excel: {output_dir / 'meta_results.xlsx'}")
    print(f"HTML:  {output_dir / 'meta_results.html'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Input CSV; original columns are preserved")
    parser.add_argument("--output-dir", default="results", help="Output directory (default: results)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Concurrent requests; effective cap is 6 (default: 4)")
    parser.add_argument("--min-interval", type=float, default=1.5,
                        help="Minimum seconds between request starts; cannot be below 1.0")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Transient network/5xx attempts, maximum 3; 403/429 are not retried")
    parser.add_argument("--export-every", type=int, default=25,
                        help="Save HTML and Excel after this many completed rows")
    args = parser.parse_args(argv)
    if args.min_interval < 1.0:
        parser.error("--min-interval cannot be less than 1 second")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not 1 <= args.max_retries <= 3:
        parser.error("--max-retries must be between 1 and 3")
    if args.export_every < 1:
        parser.error("--export-every must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted; the most recent completed 25-row snapshot remains on disk.", file=sys.stderr)
        return 130
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
