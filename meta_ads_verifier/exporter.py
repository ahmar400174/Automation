"""Background Excel + HTML snapshots. Network workers only enqueue rows."""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

from openpyxl import Workbook

COLUMNS = (
    "input",
    "page_id",
    "page_name",
    "status",
    "runs_ads",
    "ad_count",
    "page_deleted",
    "http_status",
    "attempts",
    "proxy",
    "country",
    "checked_at",
    "elapsed_ms",
    "library_url",
    "error",
)

_ILLEGAL_XLSX = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _ILLEGAL_XLSX.sub("", str(value))


def write_snapshot(rows: list[dict], xlsx_path: str, html_path: str, meta: dict) -> None:
    _write_xlsx(rows, xlsx_path)
    _write_html(rows, html_path, meta)


def _atomic(path: str, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    temporary.write_bytes(payload)
    os.replace(temporary, target)


def _write_xlsx(rows: list[dict], path: str) -> None:
    book = Workbook(write_only=True)
    sheet = book.create_sheet("verification")
    sheet.append(list(COLUMNS))
    for row in rows:
        sheet.append([_cell(row.get(column)) for column in COLUMNS])
    temporary = Path(path).with_suffix(Path(path).suffix + ".partial")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    book.save(temporary)
    os.replace(temporary, path)


def _write_html(rows: list[dict], path: str, meta: dict) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    processed = int(meta.get("processed") or len(rows))
    success = float(meta.get("success_rate") or 0.0)
    pps = float(meta.get("pps") or 0.0)
    cards = (
        ("Processed", f"{processed:,}"),
        ("Pages/sec", f"{pps:.1f}"),
        ("Pages/min", f"{pps * 60:.0f}"),
        ("Success", f"{success:.1f}%"),
        ("Active", f"{counts.get('active', 0):,}"),
        ("No ads", f"{counts.get('inactive', 0):,}"),
        ("Not found", f"{counts.get('not_found', 0):,}"),
        ("Blocked", f"{counts.get('blocked', 0) + counts.get('error', 0):,}"),
    )
    card_html = "".join(
        f"<div class='card'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in cards
    )
    body_rows = []
    for row in rows:
        status = str(row.get("status") or "")
        cells = []
        for column in COLUMNS:
            value = row.get(column)
            text = "" if value is None else str(value)
            css = f" class='st st-{html.escape(status)}'" if column == "status" else ""
            cells.append(f"<td{css}>{html.escape(text)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    headers = "".join(f"<th>{html.escape(column)}</th>" for column in COLUMNS)
    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Meta page verification</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; background: #0e1116; color: #e7ecf3; font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; }}
header {{ padding: 28px 28px 8px; }}
h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: -0.03em; }}
p.sub {{ margin: 0; color: #93a0b4; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; padding: 18px 28px; }}
.card {{ background: #171c25; border: 1px solid #2a3342; border-radius: 12px; padding: 12px 14px; }}
.card span {{ display: block; color: #93a0b4; font-size: 12px; }}
.card strong {{ font-size: 20px; }}
.tools {{ padding: 0 28px 12px; }}
input {{ width: min(420px, 100%); background: #171c25; color: inherit; border: 1px solid #2a3342; border-radius: 10px; padding: 10px 12px; }}
.wrap {{ overflow: auto; padding: 0 28px 40px; }}
table {{ width: 100%; border-collapse: collapse; min-width: 1100px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #232b39; white-space: nowrap; }}
th {{ position: sticky; top: 0; background: #121720; color: #93a0b4; font-weight: 600; }}
tr:hover td {{ background: #151b24; }}
.st {{ font-weight: 700; }}
.st-active {{ color: #3dd68c; }}
.st-inactive {{ color: #9aa8bc; }}
.st-not_found, .st-unresolved {{ color: #f0c14d; }}
.st-deleted {{ color: #d7b08a; }}
.st-blocked, .st-error {{ color: #ff6b6b; }}
</style>
</head>
<body>
<header>
  <h1>Meta Ad Library verification</h1>
  <p class="sub">Snapshot rewritten every batch while the queue keeps running. Inflight {html.escape(str(meta.get("inflight", 0)))}. Retries {html.escape(str(meta.get("retries", 0)))}.</p>
</header>
<section class="cards">{card_html}</section>
<div class="tools"><input id="q" placeholder="Filter rows" oninput="filterRows()"/></div>
<div class="wrap">
<table>
<thead><tr>{headers}</tr></thead>
<tbody id="rows">
{"".join(body_rows)}
</tbody>
</table>
</div>
<script>
function filterRows() {{
  const q = document.getElementById("q").value.toLowerCase();
  for (const tr of document.querySelectorAll("#rows tr")) {{
    tr.style.display = tr.innerText.toLowerCase().includes(q) ? "" : "none";
  }}
}}
</script>
</body>
</html>
"""
    _atomic(path, document.encode("utf-8"))
