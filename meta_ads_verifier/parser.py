"""Input normalization and Ad Library HTML parsing.

A logged-out Ad Library document embeds the verdict in
`ad_library_main.search_results_connection.count` and
`ad_library_page_info.page_info`. No second GraphQL round-trip.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import urlencode

_COUNT_RE = re.compile(
    r'"search_results_connection"\s*:\s*\{\s*"count"\s*:\s*(\d+)'
)
_PAGE_INFO_RE = re.compile(
    r'"ad_library_page_info"\s*:\s*\{\s*"page_info"\s*:\s*(null|\{[^{}]*\})'
)
_NAME_RE = re.compile(r'"page_name"\s*:\s*"((?:\\.|[^"\\])*)"')
_DELETED_RE = re.compile(r'"page_is_deleted"\s*:\s*(true|false)')
_CAPTCHA_RE = re.compile(r'"xfb_ad_library_is_captcha_required"\s*:\s*true')
_CHALLENGE_RE = re.compile(
    r"""fetch\(\s*['"]([^'"]*__rd_verify_[^'"]+)['"]""",
    re.I,
)

_HEADER_PRIORITY = (
    "page_id",
    "view_all_page_id",
    "pageid",
    "url",
    "page_url",
    "facebook_url",
    "facebook",
    "link",
    "profile_url",
    "profile",
    "page",
    "query",
    "input",
    "id",
)
_HEADER_NAMES = set(_HEADER_PRIORITY)


def classify_target(raw: str) -> tuple[str, str]:
    """Return (page_id_or_original, kind) where kind is id | unresolved | empty."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s or s.startswith("#"):
        return "", "empty"
    m = re.fullmatch(r"(?:page:)?(\d+)", s, flags=re.I)
    if m:
        return m.group(1), "id"
    m = re.search(r"view_all_page_id=(\d+)", s, flags=re.I)
    if m:
        return m.group(1), "id"
    m = re.search(r"profile\.php\?(?:.*&)?id=(\d+)", s, flags=re.I)
    if m:
        return m.group(1), "id"
    m = re.search(r"[?&]id=(\d+)", s)
    if m and "facebook.com" in s.lower():
        return m.group(1), "id"
    m = re.search(
        r"facebook\.com/(?:pages|people|pg)/[^?#]*?(\d{5,})",
        s,
        flags=re.I,
    )
    if m:
        return m.group(1), "id"
    return s, "unresolved"


def library_url(page_id: str, country: str, active_status: str) -> str:
    params = {
        "active_status": active_status,
        "ad_type": "all",
        "country": country,
        "is_targeted_country": "false",
        "media_type": "all",
        "search_type": "page",
        "view_all_page_id": page_id,
    }
    return "https://www.facebook.com/ads/library/?" + urlencode(params)


def challenge_path(html: str) -> str | None:
    if not html or "__rd_verify_" not in html:
        return None
    # Real library documents are hundreds of KB. The interstitial is ~500 bytes.
    if len(html) > 20000 and "search_results_connection" in html:
        return None
    match = _CHALLENGE_RE.search(html)
    if not match:
        return None
    path = match.group(1).replace("&", "&")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.facebook.com" + path


def _unquote(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\/", "/").replace('\\"', '"')


def parse_library_html(text: str) -> dict | None:
    """Return a verdict dict, or None when the body is not a usable library page."""
    if not text or "search_results_connection" not in text:
        return None
    if _CAPTCHA_RE.search(text):
        return None
    anchor = text.find('"ad_library_main"')
    window = text[anchor : anchor + 1200] if anchor != -1 else text
    count_match = _COUNT_RE.search(window) or _COUNT_RE.search(text)
    if not count_match:
        return None
    count = int(count_match.group(1))
    info_match = _PAGE_INFO_RE.search(text)
    exists: bool | None = None
    name = ""
    deleted = False
    if info_match:
        blob = info_match.group(1)
        if blob == "null":
            exists = False
        else:
            exists = True
            name_match = _NAME_RE.search(blob)
            deleted_match = _DELETED_RE.search(blob)
            if name_match:
                name = _unquote(name_match.group(1))
            if deleted_match:
                deleted = deleted_match.group(1) == "true"
    if exists is False and count == 0:
        status = "not_found"
        runs = "no"
    elif deleted and count == 0:
        status = "deleted"
        runs = "no"
    elif count > 0:
        status = "active"
        runs = "yes"
    else:
        status = "inactive"
        runs = "no"
    return {
        "status": status,
        "runs_ads": runs,
        "ad_count": count,
        "page_name": name,
        "page_deleted": "yes" if deleted else "no",
    }


def _norm_header(value: object) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return text


def _column_index(headers: list[str]) -> int:
    found = {name: idx for idx, name in enumerate(headers)}
    for name in _HEADER_PRIORITY:
        if name in found:
            return found[name]
    return 0


def _rows_from_headerless(cells: list[list[object]]) -> list[str]:
    out: list[str] = []
    for row in cells:
        if not row:
            continue
        value = row[0]
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out.append(text)
    return out


def load_inputs(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"input not found: {path}")
    suffix = file_path.suffix.lower()
    if suffix in {".txt", ".list"}:
        lines = file_path.read_text(encoding="utf-8-sig").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if suffix == ".csv":
        with file_path.open(encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(handle, dialect)
            rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            return []
        headers = [_norm_header(cell) for cell in rows[0]]
        if any(header in _HEADER_NAMES for header in headers):
            index = _column_index(headers)
            return [row[index].strip() for row in rows[1:] if len(row) > index and row[index].strip()]
        return _rows_from_headerless(rows)
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(file_path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            rows = [
                [("" if cell is None else str(cell)) for cell in row]
                for row in sheet.iter_rows(values_only=True)
                if row and any(cell is not None and str(cell).strip() for cell in row)
            ]
        finally:
            workbook.close()
        if not rows:
            return []
        headers = [_norm_header(cell) for cell in rows[0]]
        if any(header in _HEADER_NAMES for header in headers):
            index = _column_index(headers)
            return [
                row[index].strip()
                for row in rows[1:]
                if len(row) > index and row[index].strip()
            ]
        return _rows_from_headerless(rows)
    raise ValueError(f"unsupported input type {suffix}; use .txt, .csv, or .xlsx")
