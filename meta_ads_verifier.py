#!/usr/bin/env python3
"""Build an expandable Meta Ads report and optionally verify page URLs.

Expected CSV/DataFrame columns (common aliases are also recognized):
  page_name, page_profile, site_link, page_url, ad_id, ad_link,
  ad_preview, launch_date

The verifier uses ordinary HTTP requests, bounded concurrency, and a configurable
request rate. It does not spoof browser TLS fingerprints, rotate proxies, or retry
403/429 responses. Supply only URLs you are authorized to check, and observe the
site's terms and published rate limits.

Dependencies:
  pip install pandas aiohttp openpyxl tqdm
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import math
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd

LOG = logging.getLogger("meta_ads_verifier")

COLUMN_ALIASES = {
    "page_name": ("page_name", "competitor", "competitor_name", "name"),
    "page_profile": ("page_profile", "profile", "profile_image", "page_image"),
    "site_link": ("site_link", "website", "site", "domain"),
    "page_url": ("page_url", "url", "page_link", "facebook_url"),
    "ad_id": ("ad_id", "library_id", "id"),
    "ad_link": ("ad_link", "library_url", "ad_url"),
    "ad_preview": ("ad_preview", "preview", "image", "thumbnail", "image_url"),
    "launch_date": ("launch_date", "launched", "start_date", "ad_start_date"),
    "ad_count": ("ad_count", "number_of_ads", "count"),
}


def _column(df: pd.DataFrame, logical_name: str) -> str | None:
    names = {str(c).strip().lower(): c for c in df.columns}
    return next((names[a] for a in COLUMN_ALIASES[logical_name] if a in names), None)


def _value(row: pd.Series, col: str | None, default: Any = "") -> Any:
    if col is None:
        return default
    value = row.get(col, default)
    return default if pd.isna(value) else value


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _safe_url(value: Any) -> str:
    """Return an escaped http(s) URL, or empty string for unsafe/missing input."""
    value = _text(value)
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return html.escape(value, quote=True)


def _date_and_days(value: Any, today: date) -> tuple[str, int | None]:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "", None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return _text(value), None
    launched = parsed.date()
    return launched.isoformat(), max(0, (today - launched).days)


def _bucket(days: int | None) -> tuple[str, str]:
    if days is None:
        return "unknown", "Unknown"
    if 1 <= days <= 4:
        return "new", "New Tests"
    if 5 <= days <= 12:
        return "trajectory", "Winning Trajectory"
    if days > 12:
        return "scaling", "Scaling/Old"
    return "unknown", "Less than 1 day"


def generate_html_report(
    df: pd.DataFrame,
    output_path: str | Path,
    *,
    title: str = "Meta Ads Verification Report",
    today: date | None = None,
) -> Path:
    """Generate a standalone HTML report with expandable page rows and ad filters.

    ``df`` should contain one row per ad, with page-level values repeated for each
    ad. Missing optional columns are displayed as blank. ``launch_date`` is used
    to calculate ``Days_Live`` before rendering.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    data = df.copy()

    cols = {name: _column(data, name) for name in COLUMN_ALIASES}
    launch_col = cols["launch_date"]
    date_days = [_date_and_days(v, today) for v in data[launch_col]] if launch_col else [("", None)] * len(data)
    data["_report_launched"] = [x[0] for x in date_days]
    data["Days_Live"] = [x[1] for x in date_days]

    # Group by stable page identity where available; otherwise by displayed page name.
    group_col = cols["page_url"] or cols["page_name"] or cols["site_link"]
    if group_col:
        groups: Iterable[tuple[Any, pd.DataFrame]] = data.groupby(group_col, sort=False, dropna=False)
    else:
        groups = [("Page", data)]

    page_rows: list[str] = []
    group_index = 0
    for _, ads in groups:
        group_index += 1
        first = ads.iloc[0]
        group_id = f"page-{group_index}"
        page_name = _text(_value(first, cols["page_name"], "Unnamed page")) or "Unnamed page"
        profile = _safe_url(_value(first, cols["page_profile"]))
        site_url = _safe_url(_value(first, cols["site_link"]))
        page_url = _safe_url(_value(first, cols["page_url"]))
        active_count = len(ads)
        if cols["ad_count"]:
            counts = pd.to_numeric(ads[cols["ad_count"]], errors="coerce").dropna()
            if not counts.empty:
                active_count = int(counts.max())

        if profile:
            page_display = f'<img class="avatar" src="{profile}" alt="" loading="lazy">'
        else:
            page_display = '<span class="avatar avatar-placeholder" aria-hidden="true">P</span>'
        name_link = f'<a href="{page_url}" target="_blank" rel="noopener noreferrer">{html.escape(page_name)}</a>' if page_url else html.escape(page_name)
        site_display = f'<a href="{site_url}" target="_blank" rel="noopener noreferrer">{html.escape(_text(_value(first, cols["site_link"])))}</a>' if site_url else html.escape(_text(_value(first, cols["site_link"])))

        ad_rows: list[str] = []
        for _, ad in ads.iterrows():
            preview_url = _safe_url(_value(ad, cols["ad_preview"]))
            preview = (f'<img class="preview" src="{preview_url}" alt="Ad preview" loading="lazy">'
                       if preview_url else '<div class="preview preview-placeholder">No preview</div>')
            ad_id = _text(_value(ad, cols["ad_id"]))
            ad_href = _safe_url(_value(ad, cols["ad_link"])) or _safe_url(_value(ad, cols["page_url"]))
            link_label = f"Library ID {ad_id}" if ad_id else "Open ad"
            ad_link_html = (f'<a class="library-link" href="{ad_href}" target="_blank" rel="noopener noreferrer">{html.escape(link_label)}</a>'
                            if ad_href else html.escape(link_label))
            count_value = _value(ad, cols["ad_count"], 1)
            try:
                count_label = str(max(1, int(float(count_value))))
            except (TypeError, ValueError):
                count_label = "1"
            launched = _text(ad["_report_launched"])
            days_value = ad["Days_Live"]
            days = None if pd.isna(days_value) else int(days_value)
            bucket, label = _bucket(days)
            days_text = f"{days} d" if days is not None else "—"
            badge = f'<span class="days-badge {bucket}">{html.escape(label)}</span>'
            ad_page_url = _safe_url(_value(ad, cols["page_url"]))
            page_anchor = (f'<a href="{ad_page_url}" target="_blank" rel="noopener noreferrer">{html.escape(_text(_value(ad, cols["page_url"])))}</a>'
                           if ad_page_url else "—")
            ad_rows.append(
                f'<tr class="ad-row" data-bucket="{bucket}">'
                f'<td>{preview}</td><td>{ad_link_html} <span class="count-badge">{count_label} ads</span></td>'
                f'<td>{page_anchor}</td><td>{html.escape(launched) or "—"}</td>'
                f'<td><strong>{days_text}</strong> {badge}</td></tr>'
            )

        page_rows.append(
            f'<tr class="page-row" data-group="{group_id}" tabindex="0" role="button" aria-expanded="false">'
            f'<td><span class="chevron" aria-hidden="true">▸</span>{page_display}</td>'
            f'<td>{name_link}</td><td>{site_display or "—"}</td><td><span class="total-badge">{active_count}</span></td></tr>'
            f'<tr class="detail-row" id="{group_id}" style="display:none"><td colspan="4"><div class="detail-wrap">'
            f'<table class="ads-table"><thead><tr><th>Ad Preview</th><th>Ad Link</th><th>Page URL</th><th>Launched</th><th>Days Live</th></tr></thead>'
            f'<tbody>{"".join(ad_rows) or "<tr><td colspan=\"5\">No ads available</td></tr>"}</tbody></table>'
            f'</div></td></tr>'
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    safe_title = html.escape(title)
    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
:root{{--ink:#172033;--muted:#697386;--line:#e4e9f0;--surface:#fff;--bg:#f5f7fb;--blue:#2563eb}}
*{{box-sizing:border-box}}body{{margin:0;padding:32px;background:var(--bg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink)}}
main{{max-width:1280px;margin:auto}}h1{{margin:0 0 6px;font-size:25px}}.subtitle{{color:var(--muted);margin:0 0 22px}}
.toolbar{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}}button{{border:1px solid var(--line);background:white;color:var(--ink);border-radius:8px;padding:8px 13px;cursor:pointer;font-weight:600}}button:hover,button.active{{background:#eaf1ff;border-color:#9bb9ff;color:#1749b2}}
.table-shell{{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow-x:auto;box-shadow:0 5px 20px #18294b0a}}table{{border-collapse:collapse;width:100%}}th,td{{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}}th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#fafbfe}}.page-row{{cursor:pointer}}.page-row:hover{{background:#f6f9ff}}.page-row:focus{{outline:2px solid #93b4ff;outline-offset:-2px}}.page-row td:first-child{{white-space:nowrap}}.chevron{{display:inline-block;width:18px;color:var(--blue);transition:transform .2s ease}}.page-row[aria-expanded="true"] .chevron{{transform:rotate(90deg)}}
a{{color:#215ac5;text-decoration:none}}a:hover{{text-decoration:underline}}.avatar{{width:34px;height:34px;border-radius:50%;object-fit:cover;vertical-align:middle;margin-right:9px}}.avatar-placeholder{{display:inline-flex;align-items:center;justify-content:center;background:#e8edf5;color:#60708a;font-weight:700}}.total-badge,.count-badge{{display:inline-block;padding:3px 9px;border-radius:999px;background:#edf2fa;color:#34445f;font-size:12px;font-weight:700}}.detail-row>td{{padding:0;background:#f9fbff}}.detail-wrap{{padding:12px 18px 18px 40px;animation:expand .2s ease-out}}@keyframes expand{{from{{opacity:0;transform:translateY(-4px)}}to{{opacity:1;transform:translateY(0)}}}}.ads-table{{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}}.ads-table th,.ads-table td{{padding:10px 12px}}.preview{{display:block;width:88px;height:56px;object-fit:cover;border-radius:6px;background:#eef1f6}}.preview-placeholder{{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px}}.library-link{{font-weight:700;white-space:nowrap}}.count-badge{{margin-left:5px}}.days-badge{{display:inline-block;margin-left:6px;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}}.days-badge.new{{background:#dcfce7;color:#166534}}.days-badge.trajectory{{background:#dbeafe;color:#1d4ed8}}.days-badge.scaling{{background:#ffedd5;color:#9a3412}}.days-badge.unknown{{background:#eef1f5;color:#667085}}.empty{{padding:25px;text-align:center;color:var(--muted)}}
@media(max-width:720px){{body{{padding:16px}}th,td{{padding:9px}}.detail-wrap{{padding:8px}}.ads-table{{min-width:760px}}}}
</style></head><body><main><h1>{safe_title}</h1><p class="subtitle">Generated {generated} · {len(data)} ad records across {group_index} pages</p>
<div class="toolbar" role="group" aria-label="Filter ads by days live"><button class="active" data-filter="all">All ads</button><button data-filter="new">New Tests · 1–4 d</button><button data-filter="trajectory">Winning Trajectory · 5–12 d</button><button data-filter="scaling">Scaling/Old · &gt;12 d</button></div>
<div class="table-shell"><table id="pages"><thead><tr><th>Page Profile</th><th>Page Name</th><th>Site Link</th><th>Total Active Ads</th></tr></thead>
<tbody>{"".join(page_rows) if page_rows else '<tr><td class="empty" colspan="4">No page/ad records found.</td></tr>'}</tbody></table></div></main>
<script>
(() => {{
  let filter = 'all';
  const pageRows = [...document.querySelectorAll('.page-row')];
  const applyFilter = () => {{
    for (const page of pageRows) {{
      const detail = document.getElementById(page.dataset.group);
      const ads = [...detail.querySelectorAll('.ad-row')];
      let visible = 0;
      for (const ad of ads) {{
        const show = filter === 'all' || ad.dataset.bucket === filter;
        ad.style.display = show ? '' : 'none';
        if (show) visible++;
      }}
      const showPage = filter === 'all' || visible > 0;
      page.style.display = showPage ? '' : 'none';
      if (!showPage) {{ detail.style.display = 'none'; page.setAttribute('aria-expanded', 'false'); }}
    }}
  }};
  const toggle = (page) => {{
    const detail = document.getElementById(page.dataset.group);
    const open = page.getAttribute('aria-expanded') !== 'true';
    page.setAttribute('aria-expanded', String(open));
    detail.style.display = open ? 'table-row' : 'none';
  }};
  pageRows.forEach(page => {{
    page.addEventListener('click', event => {{ if (!event.target.closest('a')) toggle(page); }});
    page.addEventListener('keydown', event => {{ if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('a')) {{ event.preventDefault(); toggle(page); }} }});
  }});
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {{
    filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('active', item === button));
    applyFilter();
  }}));
}})();
</script></body></html>'''
    output.write_text(document, encoding="utf-8")
    return output


class RequestPacer:
    """Simple global start-rate limiter; requests are never burst beyond the rate."""
    def __init__(self, requests_per_second: float):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        self.interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_start - now)
            self._next_start = max(now, self._next_start) + self.interval
        if delay:
            await asyncio.sleep(delay)


async def _check_url(
    session: Any,
    url: str,
    semaphore: asyncio.Semaphore,
    pacer: RequestPacer,
    retries: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {"verification_status": "error", "http_status": None, "verification_error": ""}
    if not _safe_url(url):
        result.update(verification_status="invalid_url", verification_error="URL must be a valid http(s) URL")
        return result
    for attempt in range(retries + 1):
        try:
            await pacer.wait()
            async with semaphore:
                async with session.get(url, allow_redirects=True) as response:
                    result["http_status"] = response.status
                    if 200 <= response.status < 400:
                        result["verification_status"] = "success"
                    elif response.status in (403, 429):
                        # Treat access denial/rate limiting as final. Do not evade it.
                        result["verification_status"] = "blocked" if response.status == 403 else "rate_limited"
                    elif response.status >= 500 and attempt < retries:
                        response.release()
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    else:
                        result["verification_status"] = "http_error"
                    return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["verification_error"] = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            result["verification_status"] = "error"
            return result
    return result


def _write_outputs(df: pd.DataFrame, output_prefix: Path) -> None:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path = output_prefix.with_suffix(".xlsx")
    html_path = output_prefix.with_suffix(".html")
    df.to_excel(xlsx_path, index=False)
    generate_html_report(df, html_path, title="Meta Ads Verification Report")


async def verify_dataframe(
    df: pd.DataFrame,
    output_prefix: str | Path,
    *,
    url_column: str | None = None,
    concurrency: int = 10,
    requests_per_second: float = 5.0,
    retries: int = 2,
    flush_every: int = 50,
    timeout_seconds: float = 20.0,
) -> pd.DataFrame:
    """Verify page URLs using bounded async HTTP requests and periodic exports.

    403/429 responses are recorded and not retried. Only transient network errors
    and 5xx responses receive a small, bounded exponential retry.
    """
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError("Install aiohttp with: pip install aiohttp") from exc
    if concurrency < 1 or retries < 0 or flush_every < 1:
        raise ValueError("concurrency/flush_every must be >= 1 and retries >= 0")
    result_df = df.copy().reset_index(drop=True)
    selected_url = url_column or _column(result_df, "page_url")
    if selected_url is None or selected_url not in result_df.columns:
        raise ValueError("No page URL column found; specify --url-column")
    result_df["verification_status"] = "pending"
    result_df["http_status"] = pd.NA
    result_df["verification_error"] = ""
    output_prefix = Path(output_prefix)
    semaphore = asyncio.Semaphore(concurrency)
    pacer = RequestPacer(requests_per_second)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=max(1, min(concurrency, 5)))
    headers = {"User-Agent": "MetaAdsPageVerifier/1.0 (authorized verification)"}
    try:
        from tqdm import tqdm
        progress = tqdm(total=len(result_df), desc="Verifying", unit="page", file=sys.stderr)
    except ImportError:
        progress = None

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        async def check_row(row_index: int, url: Any) -> tuple[int, dict[str, Any]]:
            outcome = await _check_url(session, _text(url), semaphore, pacer, retries)
            return row_index, outcome

        tasks = [asyncio.create_task(check_row(row_index, url))
                 for row_index, url in enumerate(result_df[selected_url].tolist())]
        completed = 0
        try:
            for task in asyncio.as_completed(tasks):
                row_index, outcome = await task
                result_df.at[row_index, "verification_status"] = outcome["verification_status"]
                result_df.at[row_index, "http_status"] = outcome["http_status"]
                result_df.at[row_index, "verification_error"] = outcome["verification_error"]
                completed += 1
                if progress:
                    progress.update(1)
                    progress.set_postfix(done=completed, ok=int((result_df["verification_status"] == "success").sum()))
                if completed % flush_every == 0:
                    # to_thread prevents spreadsheet/HTML writes from blocking the event loop.
                    await asyncio.to_thread(_write_outputs, result_df.copy(), output_prefix)
        finally:
            if progress:
                progress.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(_write_outputs, result_df.copy(), output_prefix)
    return result_df


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Meta Ads report and optionally verify page URLs.")
    parser.add_argument("input_csv", help="CSV containing page/ad-level rows")
    parser.add_argument("--output-prefix", default="meta_ads_report", help="Output path without extension")
    parser.add_argument("--verify", action="store_true", help="Check the page URLs before writing outputs")
    parser.add_argument("--url-column", help="Column containing URLs to verify (auto-detected by default)")
    parser.add_argument("--concurrency", type=int, default=10, help="Maximum concurrent requests (default: 10)")
    parser.add_argument("--rate", type=float, default=5.0, help="Maximum request starts per second (default: 5)")
    parser.add_argument("--retries", type=int, default=2, help="Bounded retries for network/5xx errors only")
    parser.add_argument("--flush-every", type=int, default=50, help="Export progress every N completed records")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    input_path = Path(args.input_csv)
    df = pd.read_csv(input_path)
    prefix = Path(args.output_prefix)
    if args.verify:
        asyncio.run(verify_dataframe(
            df,
            prefix,
            url_column=args.url_column,
            concurrency=args.concurrency,
            requests_per_second=args.rate,
            retries=args.retries,
            flush_every=args.flush_every,
        ))
    else:
        _write_outputs(df, prefix)
    print(f"Wrote {prefix.with_suffix('.xlsx')} and {prefix.with_suffix('.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
