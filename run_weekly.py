"""
All the code in this file is open source, you can use it and modify it for your own needs.
Weekly runner for GitHub Actions.
"""
import argparse
import base64
import datetime as dt
import gzip
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import openpyxl

from scoring import clean_domain, is_active_row, is_report_candidate
from verify_active import (STATUS_FAILED, STATUS_INACTIVE, STATUS_STALE, STATUS_UNVERIFIED, STATUS_VERIFIED,
                           FRESH_HOURS)
from verify_active import adlib_url as _meta_adlib_url

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
STORE = RESULTS / "scan_data"
MANIFEST = RESULTS / "run_manifest.json"

IMG_W, ROW_H = 90, 70
MAX_IMG_OTHERS = 500

ALL_HEAD = ["Ads Library link", "Site link", "Page name", "Score", "Verified active ads", "Verification",
            "Verification checked", "API reported ads", "Raw records", "Unique ads", "Countries (ads)",
            "Known", "Other pages of this site", "Why this score", "Ad text (start)", "Preview URL"]
COL_ACTIVE = "Verified active ads"
COL_STATUS = "Verification"
COL_CHECKED = "Verification checked"
COL_CLAIM = "API reported ads"
COL_RAW = "Raw records"
COL_UNIQUE = "Unique ads"
COL_COUNTRIES = "Countries (ads)"

_CHECKPOINT_LOCK = threading.Lock()
_CHECKPOINT_CONTEXT = None
_CHECKPOINT_DONE = set()
_CHECKPOINT_FAILED = set()
_CHECKPOINT_PARTIAL = set()


def begin_incremental_run(run):
    """Initialize this process's durable, country-by-country report snapshot state."""
    global _CHECKPOINT_CONTEXT, _CHECKPOINT_DONE, _CHECKPOINT_FAILED, _CHECKPOINT_PARTIAL
    with _CHECKPOINT_LOCK:
        _CHECKPOINT_CONTEXT = dict(run)
        _CHECKPOINT_DONE, _CHECKPOINT_FAILED, _CHECKPOINT_PARTIAL = set(), set(), set()


def checkpoint_country(country, code=0):
    """Atomically refresh the report as soon as one country's export is complete."""
    global _CHECKPOINT_DONE, _CHECKPOINT_FAILED, _CHECKPOINT_PARTIAL
    with _CHECKPOINT_LOCK:
        if not _CHECKPOINT_CONTEXT:
            return
        context = _CHECKPOINT_CONTEXT
        country = str(country).upper()
        _CHECKPOINT_DONE.add(country)
        if code == 3:
            _CHECKPOINT_PARTIAL.add(country)
        elif code:
            _CHECKPOINT_FAILED.add(country)
        else:
            _CHECKPOINT_PARTIAL.discard(country)
            _CHECKPOINT_FAILED.discard(country)
        countries = list(context.get("countries") or [])
        work = Path(context["work"])
        results = Path(context["results"])
        start, end = context["from"], context["to"]
        available = [c for c in countries if c in _CHECKPOINT_DONE
                     and (work / f"competitors_{c}_{start}_{end}.xlsx").is_file()]
        if not available:
            return
        pending = [c for c in countries if c not in _CHECKPOINT_DONE]
        partial = sorted(set(pending) | _CHECKPOINT_PARTIAL)
        failed = sorted(_CHECKPOINT_FAILED)
        snapshot_input: dict[str, Any] = {k: v for k, v in context.items()
                                          if k not in ("work", "results")}
        snapshot_input.update({"failed_countries": failed, "partial_countries": partial,
                               "countries_completed": available})
        run_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(context.get("execution_id") or "current"))[:100]
        live_dir = results / "live_runs" / (run_key or "current")
        live_excel, _summary, live_raw = merge(available, start, end, work, live_dir, snapshot_input,
                                              progress_snapshot=True)
        if not live_excel:
            print(f"COUNTRY CHECKPOINT could not write a progress report: {country}.", flush=True)
            return
        live_snapshot: dict[str, Any] = dict(live_raw)
        live_snapshot.update({"countries": countries, "countries_completed": available,
                              "failed_countries": failed, "partial_countries": partial,
                              "html_path": f"results/live_runs/{run_key}/competitors_week_{start}.html",
                              "workbook_path": f"results/live_runs/{run_key}/competitors_week_{start}.xlsx"})
        write_manifest(live_snapshot, live_dir)

        excel_path, _summary, snapshot_raw = merge(available, start, end, work, results, snapshot_input)
        if excel_path:
            snapshot: dict[str, Any] = dict(snapshot_raw)
            snapshot.update({"countries": countries, "countries_completed": available,
                             "failed_countries": failed, "partial_countries": partial,
                             "html_path": f"results/competitors_week_{start}.html",
                             "workbook_path": f"results/competitors_week_{start}.xlsx"})
            write_manifest(snapshot, results)
            canonical = "canonical and live"
        else:
            canonical = "live only; previous canonical report preserved until a trusted Meta answer exists"
        print(f"COUNTRY CHECKPOINT saved: {country} ({len(available)}/{len(countries)} country workbook(s)); "
              f"partial countries: {', '.join(partial) or '-'}; {canonical}.", flush=True)


def last_week(today=None):
    today = today or dt.date.today()
    start = today - dt.timedelta(days=today.weekday() + 7)
    return start, start + dt.timedelta(days=6)


def _as_int(value, default=0):
    try:
        text = str(value).strip().replace(",", "")
        if text.startswith("~"):
            text = text[1:]
        return int(float(text))
    except (TypeError, ValueError):
        return default


PAGE_ID_RE = re.compile(r"view_all_page_id=(\d+)")


def _page_id(link):
    match = PAGE_ID_RE.search(str(link or ""))
    return match.group(1) if match else ""


def adlib_url(page_id, country):
    page_id = str(page_id or "").strip()
    if not page_id:
        return ""
    # Same url builder as the verifier: the link you click is the page that was counted.
    return _meta_adlib_url(page_id, country, active=True)


def new_execution_id():
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{run_id}-" + stamp if run_id else f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def _atomic_write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_save_workbook(workbook, path):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp.xlsx")
    workbook.save(tmp)
    os.replace(tmp, path)


def build_all_top(countries, workbooks, tab="Top", stats=None):
    stats = stats if stats is not None else {}
    for name in ("candidates", "unverified", "inactive", "stale", "failed", "other"):
        stats.setdefault(name, 0)
    sites = {}
    for country in countries:
        wb = workbooks.get(country)
        if wb is None or tab not in wb.sheetnames:
            continue
        rows = list(wb[tab].iter_rows(values_only=True))
        if len(rows) < 2:
            continue
        idx = {name: i for i, name in enumerate(rows[0]) if name}

        def get(row, name, default=None):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) and row[i] is not None else default

        for row in rows[1:]:
            if not any(value not in (None, "") for value in row):
                continue
            link = str(get(row, "Ads Library link") or "")
            page_id = _page_id(link)
            raw_active = get(row, COL_ACTIVE)
            approximate = str(raw_active or "").strip().startswith("~")
            status = str(get(row, COL_STATUS) or STATUS_UNVERIFIED).strip().lower()
            if status not in (STATUS_VERIFIED, STATUS_INACTIVE, STATUS_UNVERIFIED, STATUS_STALE, STATUS_FAILED):
                status = STATUS_UNVERIFIED
            active = _as_int(raw_active, 0) if status in (STATUS_VERIFIED, STATUS_INACTIVE) else None
            key = clean_domain(get(row, "Site link"))
            stats["candidates"] += 1
            if status == STATUS_INACTIVE:
                active = 0
                stats["inactive"] += 1
            elif status == STATUS_UNVERIFIED:
                stats["unverified"] += 1
            elif status == STATUS_STALE:
                stats["stale"] += 1
            elif status == STATUS_FAILED:
                stats["failed"] += 1
            if not is_report_candidate(key, status):
                stats["other"] += 1
                continue
            if status == STATUS_VERIFIED and not is_active_row(key, active, status):
                stats["other"] += 1
                continue
            entry = sites.setdefault(key, {"candidates": [], "countries": {}, "known": ""})
            unique = _as_int(get(row, COL_UNIQUE))
            entry["candidates"].append({"active": active, "score": _as_int(get(row, "Score")),
                                        "unique": unique, "row": row, "idx": idx, "page_id": page_id,
                                        "country": country, "status": status, "approximate": approximate})
            entry["countries"][country] = entry["countries"].get(country, 0) + unique
            if get(row, "Known") and not entry["known"]:
                entry["known"] = get(row, "Known")

    out = []
    for key, entry in sites.items():
        best = max(entry["candidates"], key=lambda c: (_as_int(c["active"], -1), c["score"], c["unique"]))
        scored = max(entry["candidates"], key=lambda c: (c["score"], _as_int(c["active"], -1), c["unique"]))
        row, idx = best["row"], best["idx"]
        score = scored["score"]

        def g(name, default=""):
            i = idx.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return default
            return row[i]

        def gs(name, default=""):
            i = scored["idx"].get(name)
            if i is None or i >= len(scored["row"]) or scored["row"][i] is None:
                return default
            return scored["row"][i]

        countries_txt = ", ".join(f"{c} {n}" for c, n in sorted(entry["countries"].items(), key=lambda x: -x[1]))
        # Keep the original website URL in exports (the normalized domain remains the dedupe key).
        # Older versions stripped https:// here, which broke clickable links and downstream checks.
        site_value = str(g("Site link") or "").strip()
        if site_value and not re.match(r"^https?://", site_value, re.I):
            site_value = "https://" + site_value
        status_labels = {
            STATUS_VERIFIED: "Verified",
            STATUS_INACTIVE: STATUS_INACTIVE,
            STATUS_UNVERIFIED: "Unverified",
            STATUS_STALE: "Stale",
            STATUS_FAILED: "Verification_failed",
        }
        display_status = status_labels.get(best["status"], str(best["status"] or "").capitalize())
        active_display = (f"~{best['active']}" if best["approximate"] and best["active"] is not None
                          else best["active"])
        cells = [adlib_url(best["page_id"], best["country"]), site_value, g("Page name"),
                 score, active_display, display_status, g(COL_CHECKED), g(COL_CLAIM),
                 g(COL_RAW), g(COL_UNIQUE), countries_txt, entry["known"],
                 g("Other pages of this site"), gs("Why this score"), g("Ad text (start)")]
        out.append({"key": key, "score": score, "active": best["active"],
                    "approximate": best["approximate"], "status": best["status"],
                    "ads": sum(entry["countries"].values()),
                    "cells": [x if x is not None else "" for x in cells], "img": g("Preview URL")})

    out.sort(key=lambda r: (-r["score"], -_as_int(r["active"], -1), -r["ads"]))
    return dedupe_by_page(out)


def dedupe_by_page(rows):
    owner, kept = set(), []
    for row in sorted(rows, key=lambda r: (-_as_int(r["active"], -1), -r["score"], -r["ads"])):
        page_id = _page_id(row["cells"][0])
        if page_id and page_id in owner:
            continue
        if page_id:
            owner.add(page_id)
        kept.append(row)
    kept.sort(key=lambda r: (-r["score"], -_as_int(r["active"], -1), -r["ads"]))
    return kept


# Downloaded + resized pictures are kept on disk (the workflow restores this folder with actions/cache), so a
# rebuild / verify run downloads only the pictures it has never seen. The cached bytes are exactly the bytes
# that were produced the first time, so the report is identical with or without the cache.
THUMB_CACHE = Path(os.environ.get("WH_THUMB_CACHE") or (HERE / ".thumb_cache"))
THUMB_WORKERS = max(1, int(os.environ.get("WH_THUMB_WORKERS", "24") or 24))


def _thumb_file(url):
    import hashlib
    return THUMB_CACHE / (hashlib.sha1(str(url).encode("utf-8")).hexdigest() + ".jpg")


def _fetch_thumb(url):
    try:
        from PIL import Image
        cached = _thumb_file(url)
        try:
            if cached.exists():
                data = cached.read_bytes()
                return data, Image.open(io.BytesIO(data)).size
        except Exception:
            pass                                   # unreadable cache file: download again
        import requests
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        image = Image.open(io.BytesIO(resp.content)).convert("RGB")
        image.thumbnail((IMG_W * 2, ROW_H * 2))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=70)
        data = buffer.getvalue()
        try:                                       # only successful downloads are cached
            THUMB_CACHE.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_name(cached.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cached)
        except OSError:
            pass
        return data, image.size
    except Exception:
        return None


def write_sheet(out, title, rows, max_images=None):
    from openpyxl.drawing.image import Image as XLImage

    ws = out.create_sheet(title)
    ws.append(["Ad picture"] + ALL_HEAD[:-1])
    for row in rows:
        ws.append([""] + list(row["cells"]))

    urls = [row["img"] or "" for row in rows]
    wanted = len(urls) if max_images is None else min(len(urls), max_images)
    pics: list[tuple[bytes, tuple[int, int]] | None] = [None] * len(rows)
    if wanted:
        with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as pool:
            for i, picture in enumerate(pool.map(lambda u: _fetch_thumb(u) if u else None, urls[:wanted])):
                pics[i] = picture

    inserted = 0
    for i, picture in enumerate(pics):
        excel_row = i + 2
        ws.row_dimensions[excel_row].height = ROW_H * 0.75
        if not picture:
            continue
        data, (width, height) = picture
        image = XLImage(io.BytesIO(data))
        scale = min(IMG_W / width, ROW_H / height)
        image.width, image.height = int(width * scale), int(height * scale)
        ws.add_image(image, f"A{excel_row}")
        inserted += 1

    ws.column_dimensions["A"].width = IMG_W / 7
    for column, width in zip("BCDEFGHIJKLMNOP", (90, 40, 32, 7, 16, 18, 20, 15, 12, 10, 18, 16, 40, 70, 80)):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "B2"
    print(f"{title}: {len(rows)} rows, {inserted} pictures")
    return pics


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Competitors __PERIOD__</title>
<style>
  :root {
    --line: #e5e7eb;
    --muted: #64748b;
    --accent: #0b57d0;
    --bg: #ffffff;
    --ink: #0f172a;
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0;
    background: #f8fafc;
    color: var(--ink);
  }
  header {
    background: #fff;
    border-bottom: 1px solid var(--line);
    padding: 16px 24px;
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
  }
  .title-group {
    display: flex;
    flex-direction: column;
    margin-right: 12px;
  }
  h1 { font-size: 20px; margin: 0; font-weight: 700; color: #0f172a; }
  .period-sub { font-size: 13px; color: var(--muted); font-weight: 500; margin-top: 2px; }

  .tabs { display: flex; gap: 8px; }
  .btn {
    font: inherit;
    padding: 8px 16px;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: #fff;
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
    color: #334155;
  }
  .btn.on { background: #0f172a; color: #fff; border-color: #0f172a; }

  input, select {
    font: inherit;
    padding: 8px 14px;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: #fff;
    font-size: 13px;
  }
  input#q { width: 280px; }

  .meta-wrapper {
    max-width: 1100px;
    margin: 20px auto 10px;
    padding: 0 10px;
  }
  .meta {
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }
  .mhead {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }
  .pill {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 9999px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .pill.ok { background: #dcfce7; color: #15803d; }
  .pill.warn { background: #fef3c7; color: #b45309; }
  .pill.bad { background: #fee2e2; color: #b91c1c; }
  .mnote { font-size: 13px; color: var(--muted); }

  .mgrid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px 24px;
  }
  .mcell {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .mk {
    font-size: 11px;
    font-weight: 700;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .mv {
    font-size: 14px;
    font-weight: 600;
    color: #0f172a;
    word-break: break-word;
  }
  .mv.bad { color: #ef4444; }

  .sub-bar {
    max-width: 1100px;
    margin: 16px auto 10px;
    padding: 0 10px;
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    color: var(--muted);
  }

  .controls-bar {
    max-width: 1100px;
    margin: 12px auto 16px;
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 12px;
  }

  .pg-btn {
    padding: 6px 14px;
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  .pg-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .pn { width: 50px; text-align: center; padding: 4px; border: 1px solid var(--line); border-radius: 4px; }

  table {
    border-collapse: collapse;
    width: 100%;
    max-width: 1100px;
    margin: 0 auto 20px;
    background: #fff;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid var(--line);
  }
  th, td {
    padding: 14px 20px;
    text-align: left;
    border-bottom: 1px solid var(--line);
    vertical-align: middle;
  }
  th { font-size: 12px; color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; background: #f8fafc; }
  tr.seen { opacity: 0.45; }
  td img, .noimg {
    width: 72px;
    height: 72px;
    object-fit: cover;
    border-radius: 8px;
    background: #f1f5f9;
  }
  .brand-title { font-weight: 600; color: #2563eb; text-decoration: none; font-size: 14px; }
  .brand-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .site-link { color: #2563eb; text-decoration: none; font-size: 14px; font-weight: 500; }
  .ads-count { font-weight: 600; font-size: 14px; color: #0f172a; }
  .ads-count .nv { font-size: 11px; font-weight: 600; color: #b45309; background: #fef3c7; border-radius: 9999px; padding: 2px 8px; margin-left: 4px; }

  .tgl { width: 34px; height: 34px; border: 1px solid var(--line); background: #fff; border-radius: 6px; cursor: pointer;
         font-size: 14px; flex: none; }
  .tgl:hover { background: #f1f5f9; }
  tr.parent { cursor: pointer; }
  tr.parent:hover td { background: #f8fafc; }
  tr.ad-sub > td { padding: 0 0 6px 54px; background: #f8fafc; }
  tr.ad-sub .ads-table { margin: 0; max-width: none; width: 100%; border: 0; border-radius: 0; background: transparent; }
  .ads-table th { background: transparent; color: #2563eb; text-transform: none; font-size: 14px; padding: 10px 14px; }
  .ads-table td { padding: 8px 14px; font-size: 14px; background: transparent; }
  .ads-table tbody tr:hover td { background: #eef2ff; }
  .ads-table img, .ads-table .noimg { width: 56px; height: 56px; }
  .lib-link { color: #2563eb; text-decoration: none; font-weight: 500; }
  .grp { display: inline-block; background: #2563eb; color: #fff; border-radius: 9999px; padding: 1px 9px; font-size: 12px; margin-left: 6px; }
  .nolink { color: var(--muted); }
  .ads-table tr.sub-ad td { background: #f1f5f9; font-size: 13px; }
  .metahint { max-width: 1100px; margin: 0 auto 10px; padding: 0 10px; font-size: 12px; color: #92400e; }
  .allc { font-size: 11px; font-weight: 600; color: #475569; background: #e2e8f0; border-radius: 9999px; padding: 2px 8px; margin-left: 8px; text-decoration: none; white-space: nowrap; }
  .allc:hover { background: #cbd5e1; }
  .pagecount { padding: 12px 20px; font-size: 13px; color: #334155; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .pagecount .big { font-size: 15px; font-weight: 800; color: #0f172a; background: #e0e7ff; border-radius: 8px; padding: 3px 10px; }
  .pagecount .warn { color: #b45309; background: #fef3c7; border-radius: 9999px; padding: 2px 10px; font-size: 12px; font-weight: 600; }
  .ads-table tr.sub-ad img, .ads-table tr.sub-ad .noimg { width: 44px; height: 44px; }
  .dl { font-weight: 700; white-space: nowrap; }
  .dl .b { display: inline-block; margin-left: 8px; padding: 2px 9px; border-radius: 9999px; font-size: 11px; font-weight: 700; }
  .dl.new .b { background: #dcfce7; color: #15803d; }
  .dl.win .b { background: #dbeafe; color: #1d4ed8; }
  .dl.old .b { background: #fee2e2; color: #b91c1c; }
  .dl.old .n { color: #b91c1c; }
  .dl.new .n { color: #15803d; }
  .dl.win .n { color: #1d4ed8; }
  .noads { padding: 12px 20px; color: var(--muted); font-size: 13px; }
  .daybar { max-width: 1100px; margin: 0 auto 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 0 10px; }
  .daybar .lbl { font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-right: 4px; }
  .dbtn { padding: 7px 14px; border: 1px solid var(--line); background: #fff; border-radius: 9999px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .dbtn.on { background: #0f172a; color: #fff; border-color: #0f172a; }
  .dbtn.new.on { background: #15803d; border-color: #15803d; }
  .dbtn.win.on { background: #1d4ed8; border-color: #1d4ed8; }
  .dbtn.old.on { background: #b91c1c; border-color: #b91c1c; }
  .matchn { font-size: 11px; font-weight: 600; color: #475569; background: #e2e8f0; border-radius: 9999px; padding: 2px 8px; margin-left: 6px; }
</style>
</head>
<body>

<header>
  <div class="title-group">
    <h1>Competitors</h1>
    <div class="period-sub">__PERIOD__</div>
  </div>
  <div class="tabs">
    <button id="bt" class="btn on">Top (__NTOP__)</button>
    <button id="bo" class="btn">Others (__NOTH__)</button>
  </div>
  <input id="q" placeholder="Search brand, URL or ad text">
  
  <label style="font-size:12px;color:var(--muted);font-weight:700;">SORT</label>
  <select id="sort">
    <option value="ads_desc">Active Ads (High → Low)</option>
    <option value="ads_asc">Active Ads (Low → High)</option>
    <option value="name_asc">Name (A → Z)</option>
    <option value="name_desc">Name (Z → A)</option>
    <option value="score_desc">Score (High → Low)</option>
    <option value="score_asc">Score (Low → High)</option>
  </select>

  <label style="font-size:12px;color:var(--muted);font-weight:700;">PER PAGE</label>
  <select id="sz">
    <option selected>100</option>
    <option>50</option>
    <option>200</option>
  </select>
</header>

<div class="meta-wrapper">
__META__
</div>

<div class="sub-bar">
  <span id="tab-count">0 competitors in this tab</span>
  <span>Clicked rows turn grey · saved in this browser</span>
</div>

<div class="metahint">If an "Ads Library" link opens "No ads match your search criteria" for many pages, Meta is throttling this connection (usually while a verify run is active). Stop the run, wait, and open the link again, or use "all countries".</div>
<div class="daybar" id="daybar">
  <span class="lbl">Days live</span>
  <button class="dbtn on" data-f="all">All ads</button>
  <button class="dbtn new" data-f="new">New Tests · 1-4 d</button>
  <button class="dbtn win" data-f="win">Winning Trajectory · 5-12 d</button>
  <button class="dbtn old" data-f="old">Scaling / Old · &gt;12 d</button>
</div>

<div class="controls-bar" id="pg1"></div>
<div id="list"></div>
<div class="controls-bar" id="pg2"></div>

<script>
var DATA = __DATA__;

var page = 1;
var curTab = "top";
var query = "";
var sortMode = "ads_desc";

var seen = {};
try {
  seen = JSON.parse(localStorage.getItem("seen_" + DATA.id) || "{}");
} catch(e) {}

function esc(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

var dayFilter = "all";
var openRows = {};

function bucket(days) {
  if (days === null || days === undefined) return "";
  if (days <= 4) return "new";          // launched today .. 4 days
  if (days <= 12) return "win";         // 5 .. 12 days
  return "old";                         // more than 12 days
}
var BUCKET_LABEL = {"new": "New test", "win": "Winning", "old": "Scaling"};

function adsOf(r) {
  var list = r.ads || [];
  if (dayFilter === "all") return list;
  return list.filter(function(a) { return bucket(a.l) === dayFilter; });
}

function adsTable(r) {
  var list = adsOf(r);
  if (!list.length) return '<div class="noads">No ads in this group.</div>';
  var h = '<table class="ads-table"><thead><tr><th>Ad preview</th><th>Ad link</th><th>Page URL</th><th>Launched</th><th>Days live</th></tr></thead><tbody>';
  list.forEach(function(a) {
    var b = bucket(a.l);
    h += '<tr><td>' + (a.i ? '<img loading="lazy" src="' + esc(a.i) + '" onerror="this.outerHTML=\'<div class=noimg></div>\'">' : '<div class="noimg"></div>') + '</td>';
    if (a.id) {
      h += '<td><a class="lib-link" target="_blank" href="https://www.facebook.com/ads/library/?id=' + encodeURIComponent(a.id) + '">Library ID ' + esc(a.id) + '</a>' +
           (a.n > 1 ? '<span class="grp">' + a.n + ' ads</span>' : '') + '</td>';
    } else {
      h += '<td><span class="nolink">Library ID not provided</span></td>';
    }
    var u = a.u ? a.u.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
    h += '<td>' + (u ? '<a class="lib-link" target="_blank" href="' + esc(a.u) + '">' + esc(u) + '</a>' : '<span class="nolink">(no link read)</span>') + '</td>';
    h += '<td>' + esc(a.d || '—') + '</td>';
    h += '<td>' + (b ? '<span class="dl ' + b + '"><span class="n">' + a.l + ' d</span><span class="b">' + BUCKET_LABEL[b] + '</span></span>' : '—') + '</td></tr>';
  });
  return h + '</tbody></table>';
}

// ---- ADDED: ads grouped by creative ("N ads" with expandable sub-ads). This adsTable replaces the earlier one. ----
function adRowCells(a, n, gid, isSub) {
  var b = bucket(a.l);
  var h = '<td style="width:44px">' + (gid ? '<button class="tgl grp-tgl" data-g="' + esc(gid) + '">' + (openRows[gid] ? '▼' : '▶') + '</button>' : '') + '</td>';
  h += '<td' + (isSub ? ' style="padding-left:34px"' : '') + '>' + (a.i ? '<img loading="lazy" src="' + esc(a.i) + '" onerror="this.outerHTML=\'<div class=noimg></div>\'">' : '<div class="noimg"></div>') + '</td>';
  if (a.id) {
    h += '<td><a class="lib-link" target="_blank" href="https://www.facebook.com/ads/library/?id=' + encodeURIComponent(a.id) + '">Library ID ' + esc(a.id) + '</a>';
  } else {
    h += '<td><span class="nolink">Library ID not provided</span>';
  }
  h += (n > 1 ? '<span class="grp">' + n + ' ads</span>' : '') + '</td>';
  var u = a.u ? a.u.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  h += '<td>' + (u ? '<a class="lib-link" target="_blank" href="' + esc(a.u) + '">' + esc(u) + '</a>' : '<span class="nolink">(no link read)</span>') + '</td>';
  h += '<td>' + esc(a.d || '—') + '</td>';
  h += '<td>' + (b ? '<span class="dl ' + b + '"><span class="n">' + a.l + ' d</span><span class="b">' + BUCKET_LABEL[b] + '</span></span>' : '—') + '</td>';
  return h;
}

function adsTable(r, key) {
  key = key || r.s || r.a;
  var list = adsOf(r);
  if (!list.length) return '<div class="noads">No ads in this group.</div>';
  var groups = [], byKey = {};
  list.forEach(function(a) {
    var g = byKey[a.g];
    if (!g) { g = byKey[a.g] = {rep: a, items: []}; groups.push(g); }
    g.items.push(a);
    if ((a.l || 0) > (g.rep.l || 0)) g.rep = a;          // the group shows its longest-running ad
  });
  var total = r.c != null ? ' · Meta shows ' + (r.ca ? '~' : '') + r.c + ' active ads on this page' : '';
  var h = '<div class="noads">' + list.length + ' ad' + (list.length === 1 ? '' : 's') + ' in this scan, in ' +
          groups.length + ' creative group' + (groups.length === 1 ? '' : 's') + total + '</div>';
  h += '<table class="ads-table"><thead><tr><th></th><th>Ad preview</th><th>Ad link</th><th>Page URL</th><th>Launched</th><th>Days live</th></tr></thead><tbody>';
  groups.forEach(function(g, gi) {
    var gid = key + '#' + gi, n = g.items.length;
    h += '<tr class="ad-main">' + adRowCells(g.rep, n, n > 1 ? gid : '', false) + '</tr>';
    if (n > 1) {
      var open = !!openRows[gid];
      g.items.forEach(function(x) {
        h += '<tr class="sub-ad" data-gp="' + esc(gid) + '" style="display:' + (open ? 'table-row' : 'none') + '">' + adRowCells(x, 0, '', true) + '</tr>';
      });
    }
  });
  return h + '</tbody></table>';
}

// ADDED: opens / closes the sub-ads of a group (separate listener; the existing click handler is untouched)
document.addEventListener("click", function(e) {
  var gt = e.target.closest(".grp-tgl");
  if (!gt) return;
  var gid = gt.dataset.g;
  openRows[gid] = !openRows[gid];
  gt.textContent = openRows[gid] ? "▼" : "▶";
  Array.prototype.forEach.call(document.querySelectorAll("tr.sub-ad"), function(tr) {
    if (tr.dataset.gp === gid) tr.style.display = openRows[gid] ? "table-row" : "none";
  });
});

// ---- ADDED: accurate page-level ad count above the sub-ads, and an "all countries" link next to Ads Library ----
var _adsTableBase = adsTable;
adsTable = function(r, key) {
  var html = _adsTableBase(r, key);
  html = html.replace(/^<div class="noads">[\s\S]*?<\/div>/, '');       // the plain one-line summary is replaced below
  var all = r.ads || [], shown = adsOf(r), seenG = {}, groups = 0;
  shown.forEach(function(a) { if (!seenG[a.g]) { seenG[a.g] = 1; groups++; } });
  var meta = (r.c !== undefined && r.c !== null) ? r.c : null;
  var h = '<div class="pagecount">';
  if (meta !== null) h += '<span class="big">' + (r.ca ? '~' : '') + meta + ' active ads on this page</span><span>(Meta Ad Library' + (r.ck ? ', checked ' + esc(r.ck) : '') + ')</span>';
  else h += '<span class="big">Active ads on this page: not verified</span>';
  h += '<span>·</span><span><b>' + shown.length + '</b> listed below from the scan, in <b>' + groups + '</b> link' + (groups === 1 ? '' : 's') + '</span>';
  if (dayFilter !== "all") h += '<span>(filter: ' + shown.length + ' of ' + all.length + ' scanned ads)</span>';
  if (meta !== null && all.length < meta) h += '<span class="warn">scan lists ' + all.length + ' of ' + meta + ' - open Ads Library for the rest</span>';
  h += '</div>';
  return h + html;
};

var _adRowCellsBase = adRowCells;
adRowCells = function(a, n, gid, isSub) {
  var h = _adRowCellsBase(a, n, gid, isSub);
  if (n > 1) {
    h = h.replace(/<span class="grp">\d+ ads<\/span>/,
      '<span class="grp" title="Ads of this page that use the same link">' + n + ' ads on this link</span>');
  }
  return h;
};

var _renderBase = render;
render = function() {
  _renderBase();
  Array.prototype.forEach.call(document.querySelectorAll("tr.parent a.brand-title"), function(a) {
    if (a.nextElementSibling && a.nextElementSibling.classList.contains("allc")) return;
    var href = a.getAttribute("href") || "";
    if (!/[?&]country=/.test(href) || /[?&]country=ALL(&|$)/.test(href)) return;
    var link = document.createElement("a");
    link.className = "allc"; link.target = "_blank"; link.textContent = "all countries";
    link.title = "Same page, ads of every country (use it when the single-country view shows no ads)";
    link.href = href.replace(/([?&]country=)[A-Za-z]{2}/, "$1ALL");
    a.insertAdjacentElement("afterend", link);
  });
};

function getRows() {
  var list = DATA[curTab] || [];
  if (query) {
    var q = query.toLowerCase();
    list = list.filter(function(r) {
      return (r.n && r.n.toLowerCase().includes(q)) ||
             (r.s && r.s.toLowerCase().includes(q)) ||
             (r.a && r.a.toLowerCase().includes(q)) ||
             (r.t && r.t.toLowerCase().includes(q));
    });
  }

  if (dayFilter !== "all") {
    list = list.filter(function(r) { return adsOf(r).length > 0; });
  }

  list = list.slice();
  if (sortMode === "ads_desc") {
    list.sort((a,b) => ((b.c == null ? -1 : b.c) - (a.c == null ? -1 : a.c)) || ((b.e || 0) - (a.e || 0)));  // UPDATED LOGIC
  } else if (sortMode === "ads_asc") {
    list.sort((a,b) => ((a.c == null ? -1 : a.c) - (b.c == null ? -1 : b.c)) || ((a.e || 0) - (b.e || 0)));  // UPDATED LOGIC
  } else if (sortMode === "name_asc") {
    list.sort((a,b) => (a.n || '').localeCompare(b.n || ''));
  } else if (sortMode === "name_desc") {
    list.sort((a,b) => (b.n || '').localeCompare(a.n || ''));
  } else if (sortMode === "score_desc") {
    list.sort((a,b) => (b.sc || 0) - (a.sc || 0));
  } else if (sortMode === "score_asc") {
    list.sort((a,b) => (a.sc || 0) - (b.sc || 0));
  }
  return list;
}

function render() {
  var all = getRows();
  var size = parseInt(document.getElementById("sz").value) || 100;
  var pages = Math.max(1, Math.ceil(all.length / size));
  if (page > pages) page = pages;
  if (page < 1) page = 1;

  document.getElementById("tab-count").innerText = all.length + " competitors in this tab";

  var rows = all.slice((page - 1) * size, page * size);
  var html = '<table><thead><tr><th>Ads Library</th><th>Site</th><th>Active Ads</th></tr></thead><tbody>';

  rows.forEach(function(r) {
    var key = r.s || r.a;
    var isSeen = seen[key];
    var siteDisplay = r.s ? r.s.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '-';
    var siteUrl = r.s ? (r.s.startsWith('http') ? r.s : 'https://' + r.s) : '';
    var fbBase = 'https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&is_targeted_country=false&media_type=all&search_type=page&view_all_page_id=';
    var adUrl = (r.a && r.a.startsWith('http')) ? r.a : (fbBase + (r.a || ''));

    var hasAds = !!(r.ads && r.ads.length);
    var isOpen = hasAds && openRows[key];
    html += '<tr data-k="' + esc(key) + '" class="parent ' + (isSeen ? 'seen' : '') + '">';
    html += '<td><div style="display:flex;gap:14px;align-items:center;">';
    html += hasAds ? '<button class="tgl" title="Show this page\'s ads">' + (isOpen ? '▼' : '▶') + '</button>' : '<span style="width:34px;flex:none"></span>';
    html += r.i ? '<img src="' + esc(r.i) + '" onerror="this.outerHTML=\'<div class=noimg></div>\'">' : '<div class="noimg"></div>';
    html += '<div><a class="brand-title" target="_blank" href="' + esc(adUrl) + '">Ads Library</a>';
    html += '<div class="brand-sub">' + esc(r.n || '') + '</div>';
    html += '</div></div></td>';
    
    html += '<td>' + (siteUrl ? '<a class="site-link" target="_blank" href="' + esc(siteUrl) + '">' + esc(siteDisplay) + '</a>' : '-') + '</td>';
    html += '<td class="ads-count" title="Meta-reported result count; ~ means rounded by Meta. Verification: ' + esc(r.v || 'unverified') + (r.ck ? ' · checked at ' + esc(r.ck) : '') + '">' +
            (r.c !== undefined && r.c !== null ? ((r.ca ? '~' : '') + r.c) : (r.e > 0 ? '~' + r.e + ' <span class="nv">WH scan estimate · ' + esc(r.v || 'unverified') + '</span>' : '— <span class="nv">' + esc(r.v || 'unverified') + '</span>')) + '</td>';
    if (dayFilter !== "all" && hasAds) {
      html = html.replace(/<\/td>$/, ' <span class="matchn">' + adsOf(r).length + ' in filter</span></td>');
    }
    html += '</tr>';
    if (hasAds) {
      html += '<tr class="ad-sub" data-p="' + esc(key) + '" style="display:' + (isOpen || dayFilter !== "all" ? 'table-row' : 'none') + '"><td colspan="3">' + adsTable(r) + '</td></tr>';
    }
  });

  document.getElementById("list").innerHTML = html + '</tbody></table>';
  
  var pgHtml = '<button class="pg-btn" ' + (page <= 1 ? 'disabled' : '') + ' onclick="setPage(' + (page - 1) + ')">&lt; Prev</button> ' +
               '<span>Page <input class="pn" type="number" value="' + page + '" onchange="setPage(this.value)"> / ' + pages + ' (' + all.length + ' competitors)</span> ' +
               '<button class="pg-btn" ' + (page >= pages ? 'disabled' : '') + ' onclick="setPage(' + (page + 1) + ')">Next &gt;</button>';
  
  document.getElementById("pg1").innerHTML = pgHtml;
  document.getElementById("pg2").innerHTML = pgHtml;
}

function setPage(p) {
  page = parseInt(p) || 1;
  render();
  window.scrollTo(0,0);
}

document.getElementById("bt").onclick = function() {
  curTab = "top";
  this.className = "btn on";
  document.getElementById("bo").className = "btn";
  page = 1;
  render();
};

document.getElementById("bo").onclick = function() {
  curTab = "oth";
  this.className = "btn on";
  document.getElementById("bt").className = "btn";
  page = 1;
  render();
};

document.getElementById("q").oninput = function() {
  query = this.value;
  page = 1;
  render();
};

document.getElementById("sort").onchange = function() {
  sortMode = this.value;
  page = 1;
  render();
};

document.getElementById("sz").onchange = function() {
  page = 1;
  render();
};

document.getElementById("daybar").onclick = function(e) {
  var btn = e.target.closest(".dbtn");
  if (!btn) return;
  dayFilter = btn.dataset.f;
  Array.prototype.forEach.call(document.querySelectorAll(".dbtn"), function(b) {
    b.classList.toggle("on", b === btn);
  });
  page = 1;
  render();
};

document.addEventListener("click", function(e) {
  var parent = e.target.closest("tr.parent");
  if (parent && !e.target.closest("a")) {
    var pk = parent.dataset.k;
    openRows[pk] = !openRows[pk];
    var sub = parent.nextElementSibling;
    if (sub && sub.classList.contains("ad-sub")) {
      sub.style.display = (openRows[pk] || dayFilter !== "all") ? "table-row" : "none";
      var t = parent.querySelector(".tgl");
      if (t) t.textContent = openRows[pk] ? "▼" : "▶";
    }
    return;
  }
  var a = e.target.closest("a");
  if (!a) return;
  var tr = a.closest("tr");
  if (!tr || !tr.dataset.k) return;
  var k = tr.dataset.k;
  tr.classList.add("seen");
  seen[k] = 1;
  try {
    localStorage.setItem("seen_" + DATA.id, JSON.stringify(seen));
  } catch(x) {}
});

render();
</script>
</body>
</html>
"""


def _days_live(launched, today=None):
    """Whole days between the launch date and today (the report date). None when the date is unknown."""
    try:
        start = dt.date.fromisoformat(str(launched)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, ((today or dt.date.today()) - start).days)


def _load_ad_index(countries, start, end, work):
    """page_id -> list of per-ad records, read from the saved scan files (work/scan_<CC>_<start>_<end>.json)."""
    index = {}
    for country in countries:
        path = Path(work) / f"scan_{country}_{start}_{end}.json"
        if not path.exists():
            found = sorted(Path(work).glob(f"scan_{country}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            path = found[0] if found else None
        if not path:
            continue
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for brand in (state.get("brands") or {}).values():
            rows = brand.get("ad_rows") or []
            if rows:
                index.setdefault(str(brand.get("page_id") or ""), []).extend(rows)
    return index


def _pack_ads(raw_ads, today=None):
    today = today or dt.date.today()
    counted, order = {}, []
    for ad in raw_ads or []:
        # the same Library ID twice (two countries) is one ad
        key = ad.get("id") or (ad.get("d"), ad.get("u"), ad.get("i"))
        if key in counted:
            continue
        counted[key] = {"id": ad.get("id") or "", "d": ad.get("d") or "", "l": _days_live(ad.get("d"), today),
                        "u": ad.get("u") or "", "i": ad.get("i") or ""}
        order.append(key)
    ads = [counted[k] for k in order]
    ads.sort(key=lambda a: (a["l"] is None, a["l"] if a["l"] is not None else 0))   # newest first, undated last
    return ads


def _creative_key(ad):
    """ADDED: ads that point to the SAME LINK are one group: one row 'N ads on this link'. Ads whose link was not
    read fall back to the same picture."""
    link = str(ad.get("u") or "").strip().lower().split("#")[0].split("?")[0]
    link = re.sub(r"^https?://(www\.)?", "", link).rstrip("/")
    if link:
        return "url:" + link
    image = str(ad.get("i") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if image:
        return "img:" + image
    return "pg:" + str(ad.get("d") or "")


def _pack_ads(raw_ads, today=None):
    """ADDED (replaces the earlier definition above): keeps every ad, adds the creative-group key 'g'."""
    today = today or dt.date.today()
    ads, seen_ids = [], set()
    for ad in raw_ads or []:
        lib = ad.get("id") or ""
        if lib:                                   # the same Library ID twice (two countries) is one ad
            if lib in seen_ids:
                continue
            seen_ids.add(lib)
        ads.append({"id": lib, "d": ad.get("d") or "", "l": _days_live(ad.get("d"), today),
                    "u": ad.get("u") or "", "i": ad.get("i") or "", "g": _creative_key(ad)})
    ads.sort(key=lambda a: (a["l"] is None, -(a["l"] or 0)))      # longest-running first, undated last
    return ads



def _pack(rows, pics, ad_index=None):
    packed = []
    for i, row in enumerate(rows):
        picture = pics[i] if i < len(pics) else None
        image = ("data:image/jpeg;base64," + base64.b64encode(picture[0]).decode()) if picture else (row["img"] or "")
        cells = row["cells"]
        packed.append({"k": row["key"], "a": cells[0], "s": cells[1], "n": cells[2], "i": image,
                       "c": row["active"], "ca": bool(row.get("approximate")),
                       "sc": row["score"], "t": (cells[14] or "")[:400],
                       "v": row.get("status") or "", "domain": row["key"],
                       "ck": str(cells[6] or "")[:16].replace("T", " "),   # when Meta was asked (hover on the number)
                       "ads": _pack_ads((ad_index or {}).get(_page_id(cells[0]))),
                       "e": _as_int(row.get("ads"), 0)})  # UPDATED LOGIC: WinningHunter scan count, shown only as a labelled estimate
    return packed


def _readable(iso, with_time=True):
    try:
        stamp = dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        try:
            stamp = dt.datetime.fromisoformat(str(iso) + "T00:00:00")
        except (TypeError, ValueError):
            return str(iso or "")
    return stamp.strftime("%d %b %Y, %H:%M" if with_time else "%d %b %Y")


def _meta_html(meta):
    outcome = str(meta.get("outcome") or "unknown")
    badge = {"complete": "ok", "empty": "warn", "incomplete": "warn", "failed": "bad"}.get(outcome, "warn")

    def cell(label, value, cls="", title=""):
        tip = f' title="{html.escape(str(title))}"' if title else ""
        return (f'<div class="mcell"><span class="mk">{html.escape(str(label))}</span>'
                f'<span class="mv {cls}"{tip}>{html.escape(str(value))}</span></div>')

    stale = meta.get("freshness") == "stale"
    verified_at = str(meta.get("verified_at") or "")
    cells = [
        cell("Execution", meta.get("execution_id") or "unknown"),
        cell("Scanned", f"{_readable(meta.get('from'), False)} → {_readable(meta.get('to'), False)}"
                        f" · {meta.get('countries')}", title=f"{meta.get('from')} to {meta.get('to')}"),
        cell("Scraped at", _readable(meta.get("scraped_at")) if meta.get("scraped_at")
             else "not recorded (legacy scan)", title=meta.get("scraped_at") or ""),
        cell("Verified at", _readable(verified_at) if verified_at else "no check in this run",
             "bad" if stale or not verified_at else "", title=verified_at),
        cell("Source", meta.get("source") or ""),
        cell("Count interpretation", "Meta-displayed count; a leading ~ means Meta rounded the number"),
        cell("Competitors listed", meta.get("reported_competitors", 0)),
        cell("Verified competitors", meta.get("verified_competitors", 0)),
        cell("Verified active ads", meta.get("verified_active_ads", 0)),
        cell("Verification progress",
             f"{_as_int(meta.get('verified_pages')):,} of {_as_int(meta.get('pages_in_scope')):,} pages "
             f"({meta.get('verification_percent', 100)}%)" if meta.get("pages_in_scope") else "all pages verified"),
        cell("Inactive pages", meta.get("inactive_pages", 0)),
        cell("Unverified pages", meta.get("unverified_pages", 0)),
        cell("Stale pages", meta.get("stale_pages", 0)),
        cell("Verification failures", meta.get("verification_failed", 0)),
        cell("Pages scanned", meta.get("pages_scanned", 0)),
        cell("Duplicate records merged", meta.get("duplicate_records", 0)),
        cell("Records without an id", meta.get("missing_id_records", 0)),
        cell("Raw records scanned", meta.get("raw_records", 0)),
        cell("Unique ads scanned", meta.get("unique_ads", 0) if meta.get("dedupe_available")
             else "not recorded (legacy scan)"),
    ]
    partial = ('<span class="pill warn" title="Some pages could not be checked on Meta and are not listed">'
               'PARTIAL VERIFICATION</span>' if meta.get("verification") == "partial" else "")
    return (f'<section class="meta"><div class="mhead">'
            f'<span class="pill {badge}">{html.escape(outcome.upper())}</span>{partial}'
            f'<span class="mnote">{html.escape(str(meta.get("note") or ""))}</span></div>'
            f'<div class="mgrid">{"".join(cells)}</div></section>')


def write_html(path, start, end, top_rows, top_pics, oth_rows, oth_pics, meta=None, ad_index=None):
    meta = dict(meta or {})
    meta.setdefault("empty_note", "No competitor could be verified as running active ads in this run.")
    data = {"id": str(start), "top": _pack(top_rows, top_pics, ad_index), "oth": _pack(oth_rows, oth_pics, ad_index),
            "meta": {"outcome": meta.get("outcome"), "empty_note": meta["empty_note"]}}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (HTML_PAGE
            .replace("__PERIOD__", html.escape(f"{start} to {end}"))
            .replace("__NTOP__", str(len(top_rows)))
            .replace("__NOTH__", str(len(oth_rows)))
            .replace("__META__", _meta_html(meta))
            .replace("__DATA__", payload))
    _atomic_write_text(path, page)
    print(f"HTML saved: {path}")


def validate_report(rows):
    problems = []
    for row in rows:
        count = _as_int(row.get("active"), -1)
        status = str(row.get("status"))
        cells_active = _as_int(row["cells"][4], -1)
        if count != cells_active:
            problems.append(f"{row.get('key')}: displayed count disagrees with the record")
        if status == STATUS_VERIFIED:
            if count < 1:
                problems.append(f"{row.get('key')}: verified row with no live count")
        elif status == STATUS_INACTIVE:
            if count != 0:
                problems.append(f"{row.get('key')}: {status} row with count {count}")
        elif status in (STATUS_UNVERIFIED, STATUS_STALE, STATUS_FAILED):
            if count != -1 or cells_active != -1:
                problems.append(f"{row.get('key')}: {status} row must not claim an active count")
        else:
            problems.append(f"{row.get('key')}: unknown status {status}")
    keys = [row["key"] for row in rows]
    if len(keys) != len(set(keys)):
        problems.append("duplicate site rows")
    return problems


def describe_run(run, rows, stats, scan=None):
    scan = scan or {}
    finished = dt.datetime.now(dt.timezone.utc)
    checks = [str(r["cells"][6]) for r in rows if len(r["cells"]) > 6 and r["cells"][6]]
    verified_at = max(checks) if checks else ""
    fresh = False
    if verified_at:
        try:
            checked = dt.datetime.fromisoformat(verified_at)
            if checked.tzinfo is not None:
                checked = checked.astimezone().replace(tzinfo=None)
            age = (dt.datetime.now() - checked).total_seconds() / 3600.0
            fresh = age <= FRESH_HOURS
        except (TypeError, ValueError):
            fresh = False
    n = len(rows)
    meta = {
        "execution_id": run.get("execution_id") or "",
        "from": run.get("from"), "to": run.get("to"),
        "countries": ", ".join(run.get("countries") or []),
        "started_at": run.get("started_at") or "",
        "finished_at": finished.isoformat(timespec="seconds"),
        "timezone": "UTC",
        "source": "WinningHunter API (scan) + Meta Ad Library (verification)",
        "reported_competitors": n,
        "verified_competitors": sum(1 for r in rows if r.get("status") in (STATUS_VERIFIED, STATUS_INACTIVE)),
        # UPDATED LOGIC: one clear progress figure in the SAME unit (pages per country) as "unverified pages"
        "verified_pages": (_as_int(scan.get("verified")) + _as_int(scan.get("inactive"))) if scan else 0,
        "verified_active_ads": sum(_as_int(r["active"]) for r in rows
                                    if r.get("status") in (STATUS_VERIFIED, STATUS_INACTIVE)),
        "pages_scanned": _as_int(scan.get("pages")),
        "inactive_pages": _as_int(scan.get("inactive")) if scan else stats.get("inactive", 0),
        "unverified_pages": _as_int(scan.get("unverified")) if scan else stats.get("unverified", 0),
        "stale_pages": _as_int(scan.get("stale")) if scan else stats.get("stale", 0),
        "verification_failed": _as_int(scan.get("failed")) if scan else stats.get("failed", 0),
        "raw_records": _as_int(scan.get("raw_records")) if scan else sum(_as_int(r["cells"][8]) for r in rows),
        "unique_ads": _as_int(scan.get("unique_ads")) if scan else sum(_as_int(r["cells"][9]) for r in rows),
        "dedupe_available": bool(_as_int(scan.get("dedupe"))),
        "pages_scanned_known": bool(_as_int(scan.get("pages"))),
        "verified_at": verified_at,
        "scraped_at": scan.get("scraped_at") or "",
        "duplicate_records": _as_int(scan.get("duplicate_records")),
        "missing_id_records": _as_int(scan.get("missing_id_records")),
        "freshness": "fresh" if fresh else ("stale" if verified_at else "missing"),
        "outcome": "complete" if n else "empty",
        "note": "",
    }
    in_scope = meta["verified_pages"] + meta["unverified_pages"] + meta["stale_pages"] + meta["verification_failed"]
    meta["pages_in_scope"] = in_scope
    meta["verification_percent"] = round(100.0 * meta["verified_pages"] / in_scope, 1) if in_scope else 100.0
    # Pages Meta could not answer for. The per-country workbooks already drop them, so the merge-level
    # stats alone read 0: the scan-level counters (meta fields) are the ones that tell the truth.
    unclear = max(stats.get("unverified", 0) + stats.get("stale", 0) + stats.get("failed", 0),
                  meta["unverified_pages"] + meta["stale_pages"] + meta["verification_failed"])
    if not n:
        if not stats.get("candidates", 0) and not _as_int(scan.get("pages")):
            meta["outcome"] = "empty"
            meta["note"] = ("No ads returned by the data source for this period "
                            "(competitors already reported in earlier weeks are skipped).")
            meta["empty_note"] = ("No new ad was returned by the data source for this period "
                                  "(competitors already reported in earlier weeks are skipped).")
        elif meta["inactive_pages"] and not unclear:
            meta["note"] = "Verified active ads: 0 — every candidate page was checked and runs no active ad."
            meta["empty_note"] = "No ad is currently running on any candidate page (verified inactive)."
        elif stats.get("failed", 0):
            meta["note"] = "Verification failed — no reliable active count could be obtained."
            meta["empty_note"] = "Verification failed. This is not a zero-ad result."
        else:
            meta["note"] = "Verification incomplete — some pages could not be checked on Meta."
            meta["empty_note"] = "No competitor could be verified as running active ads in this run."
    elif not fresh:
        meta["note"] = "Stale verification: the Meta answers are older than the freshness window."
    missing = list(run.get("failed_countries") or []) + list(run.get("partial_countries") or [])
    meta["verification"] = "partial" if (n and unclear) else "full"
    if n and unclear:
        # The published rows are all verified. The pages Meta could not answer for are NOT shown (never as a
        # fake 0); they are counted here and in the header, so the gap is visible without hiding the report.
        meta["note"] = (f"{unclear} listed page(s) have no verified Active Ads number yet because Meta could "
                        f"not be checked for them (unverified {meta['unverified_pages']}, stale "
                        f"{meta['stale_pages']}, failed {meta['verification_failed']}); they show a dash. "
                        + meta["note"]).strip()
    if n and missing:
        meta["outcome"] = "incomplete"
        meta["note"] = ("Incomplete scan - not fully scanned: " + ", ".join(missing) + ". "
                        + meta["note"]).strip()
    return meta


def merge(countries, start, end, work, results, run=None, progress_snapshot=False):
    run = dict(run or {})
    summary, workbooks = [], {}
    for c in countries:
        path = work / f"competitors_{c}_{start}_{end}.xlsx"
        if not path.exists():
            summary.append((c, None))
            continue
        wb = openpyxl.load_workbook(path)
        workbooks[c] = wb
        summary.append((c, max(wb["Top"].max_row - 1, 0) if "Top" in wb.sheetnames else 0))
    if not workbooks:
        return None, summary, dict(run, outcome="failed", note="no per-country workbook was produced")

    scan = {"pages": 0, "raw_records": 0, "unique_ads": 0, "verified": 0,
            "inactive": 0, "unverified": 0, "stale": 0, "failed": 0, "dedupe": 0,
            "duplicate_records": 0, "missing_id_records": 0, "scraped_at": ""}
    for wb in workbooks.values():
        if "Meta" not in wb.sheetnames:
            continue
        for row in wb["Meta"].iter_rows(values_only=True):
            if not row:
                continue
            key, value = row[0], row[1]
            if key == "scraped_at":
                if value and str(value) > scan["scraped_at"]:
                    scan["scraped_at"] = str(value)
            elif key in scan:
                scan[key] = max(scan[key], _as_int(value)) if key == "dedupe" else scan[key] + _as_int(value)

    stats = {}
    all_rows = build_all_top(countries, workbooks, "Top", stats)
    out = openpyxl.Workbook()
    active_sheet = out.active
    if active_sheet is not None:
        out.remove(active_sheet)
    top_pics = write_sheet(out, "All Top", all_rows)

    top_keys = {row["key"] for row in all_rows}
    top_pages = {_page_id(row["cells"][0]) for row in all_rows} - {""}
    other_stats = {}
    others = [row for row in build_all_top(countries, workbooks, "Others", other_stats)
              if row["key"] not in top_keys and _page_id(row["cells"][0]) not in top_pages]
    for name in stats:
        stats[name] += other_stats.get(name, 0)
    oth_pics = write_sheet(out, "All Others", others, max_images=MAX_IMG_OTHERS)

    audit = out.create_sheet("Meta")
    audit.append(["key", "value"])
    audit.append(["execution_id", run.get("execution_id") or ""])
    for key in ("pages", "raw_records", "unique_ads", "dedupe", "duplicate_records",
                "missing_id_records", "scraped_at", "verified", "inactive", "unverified",
                "stale", "failed"):
        audit.append([key, scan.get(key, 0)])

    rows = all_rows + others
    problems = validate_report(rows)
    if problems:
        print("REPORT VALIDATION FAILED:", "; ".join(problems[:5]))
        return None, summary, dict(run, outcome="failed", note=f"report failed validation: {problems[0]}")

    # Unverified competitors are listed next to the verified ones. But a run in which NOT ONE page got a
    # trustworthy Meta answer has nothing to stand on: it must not replace the last valid report.
    trusted = [r for r in rows if r.get("status") in (STATUS_VERIFIED, STATUS_INACTIVE)]
    meta = describe_run(run, rows if (trusted or progress_snapshot) else [], stats,
                        scan if any(scan.values()) else None)

    if progress_snapshot:
        meta["outcome"] = "incomplete"
        meta["verification"] = "partial"
        meta["note"] = ("Live progress snapshot. Unknown or unanswered pages are shown as unknown, not as zero. "
                        "The canonical last-valid report is preserved until a trusted Meta answer is available.")
    elif not trusted:
        print("Report NOT published: no page has a verified Meta answer in this run. "
              "The previous valid report is kept untouched.")
        return None, summary, dict(run, **meta)

    results.mkdir(parents=True, exist_ok=True)
    excel_path = results / f"competitors_week_{start}.xlsx"
    try:
        _atomic_save_workbook(out, excel_path)
        print(f"Excel saved: {excel_path}")
    except Exception as exc:
        print("Report NOT published: Excel could not be saved:", exc)
        return None, summary, dict(run, outcome="failed", note=f"workbook save failed: {exc}")
    write_html(results / f"competitors_week_{start}.html", start, end, all_rows, top_pics,
               others, oth_pics, meta, ad_index=_load_ad_index(countries, start, end, work))

    summary.append(("ALL", len(all_rows)))
    run.update(meta)
    return excel_path, summary, run


def restore_progress(country, start, end, work):
    """Restore scan_{country}_{start}_{end}.json from the gzipped store.

    UPDATED LOGIC: if the exact end-date file is missing, also try neighbouring end dates
    (±1/±2 days) and any stored file that starts with scan_{country}_{start}_.
    The UI "Fill last week" and Python last_week() can disagree by one day
    (Sun vs Mon boundary), which previously made every country fail with
    "no per-country workbook" and left Top + Others forever unverified.
    """
    work = Path(work)
    candidates = [f"scan_{country}_{start}_{end}.json"]
    try:
        end_d = dt.date.fromisoformat(str(end))
        for delta in (-1, 1, -2, 2):
            alt = end_d + dt.timedelta(days=delta)
            candidates.append(f"scan_{country}_{start}_{alt}.json")
    except ValueError:
        pass
    try:
        for path in sorted(STORE.glob(f"scan_{country}_{start}_*.json.gz")):
            plain = path.name[:-3]  # drop .gz
            if plain not in candidates:
                candidates.append(plain)
    except OSError:
        pass

    preferred = work / f"scan_{country}_{start}_{end}.json"
    for name in candidates:
        source = STORE / (name + ".gz")
        target = work / name
        if preferred.exists():
            return
        if source.exists():
            preferred.parent.mkdir(parents=True, exist_ok=True)
            with gzip.GzipFile(filename=str(source), mode="rb") as gz, open(preferred, "wb") as plain:
                shutil.copyfileobj(gz, plain)
            if name != preferred.name:
                print(f"{country}: previous progress restored from {name} "
                      f"(end-date adjusted to {end})", flush=True)
            else:
                print(f"{country}: previous progress restored", flush=True)
            return
        if target.exists() and target != preferred:
            shutil.copy2(target, preferred)
            return


def save_progress(country, start, end, work):
    name = f"scan_{country}_{start}_{end}.json"
    source = work / name
    if source.exists():
        STORE.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as plain, gzip.GzipFile(filename=str(STORE / (name + ".gz")), mode="wb") as gz:
            shutil.copyfileobj(plain, gz)


def fresh_workdir(work):
    if work.exists():
        for path in sorted(work.glob("scan_*.json")):
            try:
                STORE.mkdir(parents=True, exist_ok=True)
                with open(path, "rb") as plain, gzip.GzipFile(filename=str(STORE / (path.name + ".gz")), mode="wb") as gz:
                    shutil.copyfileobj(plain, gz)
            except OSError as exc:
                print(f"WARNING: could not stash {path.name}: {exc}")
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)


def _run(cmd, cwd, env=None, prefix=""):
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    if proc.stdout is None:
        proc.wait()
        return 127
    for line in proc.stdout:
        checkpoint = re.search(r"COUNTRY PROGRESS CHECKPOINT:\s*([A-Z]{2})\s+\d+%", line)
        if checkpoint:
            # The child has atomically replaced this country's workbook before emitting the marker.
            checkpoint_country(checkpoint.group(1), 3)
        print(f"{prefix}{line.rstrip()}", flush=True)
    return proc.wait()


def _parallel_countries():
    return os.environ.get("WH_PARALLEL_COUNTRIES", "").strip().lower() in ("1", "true", "yes", "on")


def _country_workers(n):
    """How many countries run at the same time.

    Keep concurrency bounded: each country uses the configured steady route; five workers is the hard cap.
    Tor exit rotation is not used as a throughput mechanism.
    """
    try:
        cap = int(os.environ.get("WH_COUNTRY_WORKERS", "5") or 5)
    except ValueError:
        cap = 5
    return max(1, min(n, max(1, cap), 5))


def _country_tab_allocation(n):
    """Allocate at most six live tabs across the countries that can run concurrently."""
    n = max(1, int(n))
    active = _country_workers(n)
    try:
        per_country_cap = max(1, min(4, int(os.environ.get("WH_VERIFY_WORKERS", "4") or 4)))
    except ValueError:
        per_country_cap = 4
    slots = min(5, active * per_country_cap)
    allocation = [1] * n
    extras = max(0, slots - active)
    while extras:
        changed = False
        for index in range(active):
            if extras <= 0:
                break
            if allocation[index] < per_country_cap:
                allocation[index] += 1
                extras -= 1
                changed = True
        if not changed:
            break
    return allocation


def scan_countries(countries, start, end, work, max_calls, max_minutes, resume=False):
    """Returns (failed, partial). partial = scan stopped early but a partial workbook was exported."""
    if _parallel_countries() and len(countries) > 1:
        return _scan_countries_parallel(countries, start, end, work, max_calls, max_minutes, resume)
    failed, partial = [], []
    deadline = time.time() + max_minutes * 60
    # Every run starts from a clean slate: no earlier report data is merged in, and no store is hidden
    # because an earlier week already showed it (the "seen" history is OFF unless WH_SEEN_FILE is set).
    env = dict(os.environ, WH_DEADLINE=str(deadline), PYTHONUNBUFFERED="1")
    for position, country in enumerate(countries, start=1):
        if time.time() >= deadline:
            failed.extend(countries[position - 1:])
            break
        if resume:
            restore_progress(country, start, end, work)      # only when explicitly asked (--resume)
        env["WH_COUNTRIES_LEFT"] = str(len(countries) - position + 1)
        code = _run([sys.executable, str(HERE / "wh_scan.py"), "--country", country,
                     "--from", str(start), "--to", str(end), "--max-calls", str(max_calls),
                     "--allow-partial"],
                    cwd=work, env=env, prefix=f"[{country}] ")
        save_progress(country, start, end, work)
        checkpoint_country(country, code)
        if code == 3:
            partial.append(country)
        elif code != 0:
            failed.append(country)
    return failed, partial


def _scan_one(country, start, end, work, max_calls, deadline, resume, tabs=1):
    if resume:
        restore_progress(country, start, end, work)
    env = dict(os.environ, WH_DEADLINE=str(deadline), PYTHONUNBUFFERED="1", WH_COUNTRIES_LEFT="1",
               WH_VERIFY_WORKERS=str(tabs), WH_BATCH_PARALLEL=str(tabs), WH_PARALLEL_TAB_CAP=str(tabs))
    code = _run([sys.executable, str(HERE / "wh_scan.py"), "--country", country,
                 "--from", str(start), "--to", str(end), "--max-calls", str(max_calls),
                 "--allow-partial"],
                cwd=work, env=env, prefix=f"[{country}] ")
    save_progress(country, start, end, work)
    checkpoint_country(country, code)
    return country, code


def _scan_countries_parallel(countries, start, end, work, max_calls, max_minutes, resume=False):
    """Every country runs at the same time. Each one gets the full time budget (WH_COUNTRIES_LEFT=1)."""
    deadline = time.time() + max_minutes * 60
    print(f"Parallel scan: {len(countries)} countries at once "
          f"({', '.join(countries)}), workers={_country_workers(len(countries))}", flush=True)
    failed, partial = [], []
    with ThreadPoolExecutor(max_workers=_country_workers(len(countries))) as pool:
        tabs = _country_tab_allocation(len(countries))
        futures = [pool.submit(_scan_one, c, start, end, work, max_calls, deadline, resume, tabs[i])
                   for i, c in enumerate(countries)]
        for fut in futures:
            country, code = fut.result()
            if code == 3:
                partial.append(country)
            elif code != 0:
                failed.append(country)
    return failed, partial


def export_countries(countries, start, end, work, max_minutes=335):
    """Re-exports the per-country workbooks from the saved scan data (no API credit spent).

    With WH_VERIFY_LIVE=1 (workflow mode "verify") the pages without a fresh Meta answer are checked on
    Meta again. Sequential mode gives each country an equal share of the time left. Parallel mode
    (WH_PARALLEL_COUNTRIES=1) runs every country at the same time and each country gets the full budget.
    """
    if _parallel_countries() and len(countries) > 1:
        return _export_countries_parallel(countries, start, end, work, max_minutes)
    failed = []
    deadline = time.time() + max_minutes * 60 if max_minutes > 0 else 0
    for position, country in enumerate(countries, start=1):
        # NEVER skipped because of the deadline: a skipped country vanished from the report and from the
        # "pages left" figure (the number jumped between rounds). wh_scan builds its workbook from the cache and
        # simply does not ask Meta when this round's time is used up.
        restore_progress(country, start, end, work)
        name = work / f"scan_{country}_{start}_{end}.json"
        if not name.exists():
            failed.append(country)
            checkpoint_country(country, 1)
            continue
        code = _run([sys.executable, str(HERE / "wh_scan.py"), "--export-only", "--country", country,
                     "--from", str(start), "--to", str(end)],
                    cwd=work,
                    env=dict(os.environ, PYTHONUNBUFFERED="1", WH_DEADLINE=str(deadline),
                             WH_COUNTRIES_LEFT=str(len(countries) - position + 1)),
                    prefix=f"[{country}] ")
        if code != 0:
            failed.append(country)
        checkpoint_country(country, code)
    if failed and len(failed) == len(countries):
        _diagnose_missing_scan_data(countries, start, end)
    return failed


def _diagnose_missing_scan_data(countries, start, end):
    """Print a clear hint when every country failed because scan_*.json is missing."""
    print("\n!! No scan data found for any country. Verify/rebuild cannot run without it.", flush=True)
    print(f"   Looking for: results/scan_data/scan_{{CC}}_{start}_{end}.json.gz", flush=True)
    try:
        stored = sorted(p.name for p in STORE.glob("scan_*.json.gz")) if STORE.exists() else []
    except OSError:
        stored = []
    if not stored:
        print("   Store is empty. You must run a full SCAN first (mode=scan, needs API key + credits).",
              flush=True)
    else:
        matching = [n for n in stored if f"_{start}_" in n]
        print(f"   Store has {len(stored)} file(s). Matching start={start}: "
              f"{matching[:12] or 'none'}", flush=True)
        if not matching:
            starts = sorted({n.split("_")[2] for n in stored if n.count("_") >= 3})
            print(f"   Available start dates in store: {starts}", flush=True)
            print("   Tip: set From/To to one of those periods (use the Continue button on the dashboard).",
                  flush=True)
        pattern = re.compile(r"^scan_([A-Z]{2})_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")
        for country in countries:
            periods = []
            for name in stored:
                match = pattern.match(name)
                if match and match.group(1) == str(country).upper():
                    periods.append(f"{match.group(2)} to {match.group(3)}")
            print(f"   {country}: {', '.join(sorted(periods)) if periods else 'no saved scan data'}", flush=True)


def resolve_archived_period(countries, start, end, store=None):
    """Return the newest scan period shared by selected countries when the requested one is absent.

    Re-export/verify often starts from today's date even though the saved WinningHunter scan was made
    for an earlier period. Reusing an archived period is safe for verification: it performs no new scan
    and preserves the period belonging to the source data in the resulting report.
    """
    store = Path(store or STORE)
    requested = (str(start), str(end))
    if not store.is_dir():
        return None
    by_country = {str(country).upper(): set() for country in countries}
    pattern = re.compile(r"^scan_([A-Z]{2})_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")
    try:
        for path in store.glob("scan_*.json.gz"):
            match = pattern.match(path.name)
            if match and match.group(1) in by_country:
                by_country[match.group(1)].add((match.group(2), match.group(3)))
    except OSError:
        return None
    if not by_country or any(not periods for periods in by_country.values()):
        return None
    shared = set.intersection(*(periods for periods in by_country.values()))
    if requested in shared:
        return requested
    if not shared:
        return None
    try:
        requested_start = dt.date.fromisoformat(requested[0])
        requested_end = dt.date.fromisoformat(requested[1])
        if requested_end < requested_start:
            return None

        def rank(period):
            period_start, period_end = map(dt.date.fromisoformat, period)
            overlap = max(0, (min(period_end, requested_end) - max(period_start, requested_start)).days + 1)
            starts_before_request_end = period_start <= requested_end
            # Prefer the newest archive that has started by the requested end; overlap breaks ties.
            return (starts_before_request_end, overlap, period_start, period_end)

        return max(shared, key=rank)
    except ValueError:
        return None


def _export_one(country, start, end, work, deadline, tabs=1):
    restore_progress(country, start, end, work)
    name = work / f"scan_{country}_{start}_{end}.json"
    if not name.exists():
        checkpoint_country(country, 1)
        return country
    code = _run([sys.executable, str(HERE / "wh_scan.py"), "--export-only", "--country", country,
                 "--from", str(start), "--to", str(end)],
                cwd=work,
        env=dict(os.environ, PYTHONUNBUFFERED="1", WH_DEADLINE=str(deadline), WH_COUNTRIES_LEFT="1",
                 WH_VERIFY_WORKERS=str(tabs), WH_BATCH_PARALLEL=str(tabs), WH_PARALLEL_TAB_CAP=str(tabs)),
                prefix=f"[{country}] ")
    checkpoint_country(country, code)
    return country if code != 0 else None


def _export_countries_parallel(countries, start, end, work, max_minutes):
    deadline = time.time() + max_minutes * 60 if max_minutes > 0 else 0
    print(f"Parallel verify: {len(countries)} countries at once "
          f"({', '.join(countries)}), workers={_country_workers(len(countries))}", flush=True)
    failed = []
    with ThreadPoolExecutor(max_workers=_country_workers(len(countries))) as pool:
        tabs = _country_tab_allocation(len(countries))
        futures = [pool.submit(_export_one, c, start, end, work, deadline, tabs[i])
                   for i, c in enumerate(countries)]
        for fut in futures:
            bad = fut.result()
            if bad:
                failed.append(bad)
    if failed and len(failed) == len(countries):
        _diagnose_missing_scan_data(countries, start, end)
    return failed


def unverified_left(country, start, end, work):
    """How many pages of a country's workbook still have no Meta answer (unverified + stale + failed)."""
    path = Path(work) / f"competitors_{country}_{start}_{end}.xlsx"
    if not path.exists():
        return 0
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if "Meta" not in wb.sheetnames:
                return 0
            values = {row[0]: row[1] for row in wb["Meta"].iter_rows(values_only=True) if row and row[0]}
        finally:
            wb.close()
        return sum(_as_int(values.get(key)) for key in ("unverified", "stale", "failed"))
    except Exception:
        return 0


def finish_verification(countries, start, end, work, deadline):
    """Keeps going until completion unless a finite deadline or round cap was explicitly supplied.

    Meta throttles an IP after a few hundred checks. The checker already cools down and sweeps inside one
    pass; if pages are STILL unanswered at the end of that pass, this loop waits, then runs the verification
    again for the countries concerned. Pages that already have an answer are never asked again, so every
    round only spends time on what is missing. The client never has to start a second "rebuild" by hand.
    """
    if os.environ.get("WH_VERIFY_LIVE") != "1":
        return
    rounds = int(os.environ.get("WH_VERIFY_ROUNDS", "0") or 0)
    wait_raw = os.environ.get("WH_VERIFY_ROUND_WAIT")
    try:
        wait_base = float(wait_raw) if wait_raw is not None and wait_raw.strip() else 600.0
    except ValueError:
        wait_base = 600.0
    if not (wait_base >= 0 and wait_base < float("inf")):
        wait_base = 600.0
    number = 0
    while True:
        left = [c for c in countries if unverified_left(c, start, end, work) > 0]
        if not left:
            print("Verification complete: every page has a Meta answer.", flush=True)
            return
        total = sum(unverified_left(c, start, end, work) for c in left)
        number += 1
        if rounds > 0 and number > rounds:
            print(f"After {rounds} configured round(s), {total} page(s) remain unanswered.", flush=True)
            return
        remaining = (deadline - time.time()) / 60.0 if deadline > 0 else None
        if remaining is not None and remaining < 8:
            print(f"Time budget used up: {', '.join(left)} still have unverified page(s).", flush=True)
            return
        wait = min(wait_base * min(number, 20), 1200.0)
        if remaining is not None:
            wait = min(wait, max(0.0, remaining * 60 - 300))
        suffix = f"/{rounds}" if rounds > 0 else " (no round limit)"
        print(f"\nVerification round {number}{suffix}: {', '.join(left)} still have {total} unanswered page(s). "
              f"Waiting {wait / 60:.0f} min so Meta stops throttling, then checking only those pages.",
              flush=True)
        time.sleep(wait)
        remaining = (deadline - time.time()) / 60.0 if deadline > 0 else None
        if remaining is not None and remaining < 3:
            return
        export_countries(left, start, end, work,
                         max_minutes=max(1, int(remaining)) if remaining is not None else 0)


def estimate(countries, start, end, work):
    rows, failed = [], []
    for country in countries:
        cmd = [sys.executable, str(HERE / "wh_scan.py"), "--estimate", "--country", country,
               "--from", str(start), "--to", str(end)]
        result = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
        line = next((l for l in result.stdout.splitlines() if l.startswith("ESTIMATE|")), None)
        if result.returncode != 0 or not line or line.split("|")[2] == "unknown":
            failed.append(country)
            continue
        _, _, ads, calls = line.split("|")
        rows.append((country, int(ads), int(calls)))

    total_ads, total_credits = sum(r[1] for r in rows), sum(r[2] for r in rows)
    lines = [f"## Estimate for {start} to {end}", "",
             "| Country | Ads | Credits needed |", "|---|---:|---:|"]
    lines += [f"| {c} | {ads:,} | {credits:,} |" for c, ads, credits in rows]
    lines.append(f"| **Total** | **{total_ads:,}** | **{total_credits:,}** |")
    if failed:
        lines += ["", "Estimate failed for: " + ", ".join(failed)]
    text = "\n".join(lines)
    print("\n" + text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass
    write_estimate_report(rows, failed, start, end, total_ads, total_credits)
    if failed:
        print(f"::warning::Estimate unavailable for: {', '.join(failed)}")
    return 0 if rows else 1


def write_estimate_report(rows, failed, start, end, total_ads, total_credits):
    """Estimate report (HTML + JSON) in run_artifact/, uploaded by the workflow."""
    out = HERE / "run_artifact"
    try:
        out.mkdir(parents=True, exist_ok=True)
        body = "".join(f"<tr><td>{html.escape(c)}</td><td>{ads:,}</td><td>{credits:,}</td></tr>"
                       for c, ads, credits in rows)
        warn = (f'<p style="color:#b91c1c">Estimate unavailable for: {html.escape(", ".join(failed))}</p>'
                if failed else "")
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                f"<title>Estimate {html.escape(str(start))}</title>"
                "<style>body{font-family:system-ui,sans-serif;margin:32px;color:#0f172a}"
                "table{border-collapse:collapse}th,td{border:1px solid #e5e7eb;padding:8px 18px;text-align:left}"
                "th{background:#f8fafc}</style></head><body>"
                f"<h1>Estimate</h1><p>{html.escape(str(start))} to {html.escape(str(end))} &middot; "
                f"generated {html.escape(stamp)} UTC &middot; run {html.escape(os.environ.get('GITHUB_RUN_ID') or 'local')}</p>"
                f"<table><tr><th>Country</th><th>Ads</th><th>Credits needed</th></tr>{body}"
                f"<tr><th>Total</th><th>{total_ads:,}</th><th>{total_credits:,}</th></tr></table>{warn}"
                "</body></html>")
        _atomic_write_text(out / f"estimate_report_{start}.html", page)
        data = {"from": str(start), "to": str(end), "generated_at": stamp,
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID") or "",
                "countries": [{"country": c, "ads": a, "credits": k} for c, a, k in rows],
                "failed": failed, "total_ads": total_ads, "total_credits": total_credits}
        _atomic_write_text(out / f"estimate_report_{start}.json",
                           json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"WARNING: estimate report could not be written: {exc}")


def parse_period(date_from, date_to):
    if bool(date_from) != bool(date_to):
        sys.exit("Give BOTH dates (--from and --to), or leave both empty for last week.")
    if date_from and date_to:
        start, end = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
        if end < start:
            sys.exit("The end date is before the start date.")
        return start, end
    return last_week()


# UPDATED LOGIC: tolerant country parser. Accepts "US,FR", "US, FR", a list, or a poisoned
# character-split string like "U,S,,, ,F,R" (what an old manifest produced) and always returns ["US","FR"].
def normalize_countries(value):
    if isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    tokens = [t for t in re.split(r"[\s,;|]+", str(value or "").upper()) if t]
    if tokens and all(len(t) == 1 for t in tokens):
        joined = "".join(tokens)
        tokens = [joined[i:i + 2] for i in range(0, len(joined), 2)]
    out = []
    for t in tokens:
        t = re.sub(r"[^A-Z]", "", t)
        if len(t) == 2 and t not in out:
            out.append(t)
    return out


def write_manifest(run, results_dir=None):
    explicit_dir = results_dir is not None
    results_dir = Path(results_dir) if explicit_dir else Path(MANIFEST).parent
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(run, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    targets = [results_dir / "run_manifest.json"] if explicit_dir else [Path(MANIFEST)]
    if run.get("from"):
        targets.append(results_dir / f"run_manifest_{run['from']}.json")
    for target in targets:
        try:
            _atomic_write_text(target, payload)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Weekly WinningHunter competitor scan + dashboard")
    parser.add_argument("--countries", default="US,FR,IT,ES,GB,NL")
    parser.add_argument("--max-calls", type=int, default=5000)
    parser.add_argument("--from", "--from-date", dest="date_from")
    parser.add_argument("--to", "--to-date", dest="date_to")
    parser.add_argument("--mode", choices=("estimate", "scan"), default="")
    parser.add_argument("--max-minutes", type=int, default=335)
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--rebuild-only", action="store_true")
    parser.add_argument("--re-export", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="continue an interrupted scan of the same period instead of scanning from zero")
    args = parser.parse_args()
    if args.mode == "estimate":
        args.estimate = True

    needs_key = not (args.rebuild_only or args.re_export)
    if needs_key and not os.environ.get("WH_API_KEY"):
        sys.exit("WH_API_KEY is missing.")

    start, end = parse_period(args.date_from, args.date_to)
    countries = normalize_countries(args.countries)  # UPDATED LOGIC
    if not countries:
        sys.exit("No country given.")
    if args.re_export or args.rebuild_only:
        archived = resolve_archived_period(countries, start, end)
        if archived and archived != (str(start), str(end)):
            print(f"Requested period {start} to {end} has no matching saved scan data for "
                  f"{', '.join(countries)}. Reusing archived scan period {archived[0]} to {archived[1]} "
                  "(no WinningHunter API call); reports will show the archived period.", flush=True)
            start, end = map(dt.date.fromisoformat, archived)
        elif not archived:
            print(f"No shared archived scan period found for {', '.join(countries)}; "
                  "continuing with the requested dates so the missing-data diagnostic can list available files.",
                  flush=True)
    execution_id = new_execution_id()
    started_at = dt.datetime.now(dt.timezone.utc)
    run_base: dict[str, Any] = {"execution_id": execution_id,
                "started_at": started_at.isoformat(timespec="seconds"),
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID") or "",
                "network": os.environ.get("WH_NETWORK_PROFILE") or "",
                "from": str(start), "to": str(end), "countries": countries,
                "mode": "re-export" if args.re_export else ("rebuild" if args.rebuild_only else "scan")}

    work = HERE / "work"
    if args.rebuild_only:
        work.mkdir(parents=True, exist_ok=True)
    else:
        fresh_workdir(work)
    if args.estimate:
        sys.exit(estimate(countries, start, end, work))

    begin_incremental_run({**run_base, "work": str(work), "results": str(RESULTS)})
    failed, partial = [], []
    run_deadline = time.time() + args.max_minutes * 60 if args.max_minutes > 0 else 0
    if args.re_export:
        failed = export_countries(countries, start, end, work, args.max_minutes)
        finish_verification(countries, start, end, work, run_deadline)
    elif not args.rebuild_only:
        failed, partial = scan_countries(countries, start, end, work, args.max_calls, args.max_minutes,
                                              resume=args.resume)
        finish_verification([c for c in countries if c not in failed], start, end, work, run_deadline)
    run: dict[str, Any] = {**run_base, "failed_countries": failed, "partial_countries": partial}
    excel_path, summary, run = merge(countries, start, end, work, RESULTS, run)
    run["failed_countries"] = failed
    run["partial_countries"] = partial
    run["html_path"] = f"results/competitors_week_{start}.html" if excel_path else ""
    run["workbook_path"] = f"results/competitors_week_{start}.xlsx" if excel_path else ""
    # UPDATED LOGIC: a failed run (no per-country workbook) must not overwrite the last good
    # manifest. Overwriting used to zero out unverified_pages and make chain_control claim
    # "every page is verified" while the HTML report still showed thousands of unverified rows
    # in both Top and Others.
    if excel_path:
        write_manifest(run)
    else:
        # Still record the failure under a dated name so logs keep a trail, but leave the
        # primary run_manifest.json (the one chain_control reads) untouched.
        try:
            RESULTS.mkdir(parents=True, exist_ok=True)
            failed_path = RESULTS / f"run_manifest_failed_{run.get('from') or 'unknown'}.json"
            _atomic_write_text(failed_path, json.dumps(run, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print("Manifest NOT overwritten: this run produced no report. Previous verification counts kept.",
              flush=True)

    if failed or partial:
        print(f"::warning::Not fully scanned - failed: {', '.join(failed) or '-'}; "
              f"partial: {', '.join(partial) or '-'}")
    # The run is red only when it produced no report at all (nothing usable to hand over).
    if not excel_path and (failed or run.get("outcome") == "failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
