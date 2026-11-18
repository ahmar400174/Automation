"""Local, evidence-first verifier for CSV and Excel workbooks.

Run: python salon_verifier/app.py
No third-party packages or external service credentials are required.
"""

from __future__ import annotations

import csv
import argparse
import difflib
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .address_reader import addresses
    from .source_reader import original_csv_headers, read_table, workbook_sheets
except ImportError:
    from address_reader import addresses
    from source_reader import original_csv_headers, read_table, workbook_sheets


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT.parent
UPLOADS = ROOT / "uploads"
SELECTION = ROOT / "selection.json"


def available_files() -> list[Path]:
    return sorted([path for folder in (DATA_ROOT, UPLOADS) if folder.exists()
                   for path in folder.iterdir() if path.is_file() and not path.name.startswith("~$")
                   and path.suffix.casefold() in (".csv", ".xlsx", ".xlsm")],
                  key=lambda path: path.name.casefold())


def file_token(path: Path) -> str:
    return path.relative_to(DATA_ROOT).as_posix()


def load_settings() -> dict:
    if not SELECTION.exists():
        return {}
    try:
        value = json.loads(SELECTION.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


SETTINGS = load_settings()


def choose_source() -> Path:
    configured = os.environ.get("SALON_SOURCE_CSV")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = available_files()
    selected = SETTINGS.get("file", "")
    for path in candidates:
        if file_token(path) == selected:
            return path
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return DATA_ROOT / "No spreadsheet selected.csv"


SOURCE = choose_source()
SELECTED_SHEET = SETTINGS.get("sheet")
HEADER_ROW = int(SETTINGS.get("header_row", 0) or 0)
FIELD_OVERRIDES = SETTINGS.get("mapping", {}) if isinstance(SETTINGS.get("mapping"), dict) else {}
TABLE_CACHE: tuple | None = None
DB = ROOT / "verifier.sqlite3"
REFERENCE = ROOT / "reference.csv"
PUBLIC_FINDINGS = ROOT / "public_findings.json"
OLD_SOURCE_HASH = "d945ce368a820453225f03a4b3578accc50bfc7ad455e540cdea96375ccbdff5"
CHECK_VERSION = "website-fill-v2-addresses"
ALIASES = {
    "date": "date", "companyname": "companyname", "companywebsite": "companywebsite",
    "businessname": "companyname", "salonname": "companyname",
    "website": "companywebsite", "businesswebsite": "companywebsite", "salonwebsite": "companywebsite",
    "companyemail": "companyemail", "companyemaildomain": "companyemaildomain",
    "businessemail": "companyemail", "emailaddress": "companyemail",
    "ownername": "ownername", "owneremail": "owneremail",
    "ownerphonenumber": "ownerphone", "ownernumber": "ownerphone",
    "companynumber": "companyphone", "companyphone": "companyphone",
    "businessphone": "companyphone", "telephone": "companyphone", "phonenumber": "companyphone",
    "linkedincompanypageurl": "linkedincompany", "linkedinownerpage": "linkedinowner",
    "ownerlinkedin": "linkedinowner", "painareas": "painareas", "comments": "painareas",
    "companyaddress": "companyaddress", "industry": "industry", "city": "city",
    "businessaddress": "companyaddress", "streetaddress": "companyaddress", "address": "companyaddress",
    "state": "state", "companycountry": "companycountry", "country": "companycountry",
    "zipcode": "zipcode", "postalcode": "zipcode", "zip": "zipcode",
}
PLACEHOLDERS = {
    "not available", "no page", "none", "n/a", "na", "unknown",
    "not publicly listed", "not publicly found", "no official website",
    "no verified email found", "no verified linkedin company page found",
    "no verified linkedin page found", "same as above", "not verified", "#value!",
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d$")
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)


def missing(value: str) -> bool:
    s = value.strip().lower()
    return not s or s in PLACEHOLDERS or s.startswith(("not available ", "not verified "))


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def field_kind(field: str) -> str:
    if field in FIELD_OVERRIDES:
        return FIELD_OVERRIDES[field]
    clean = re.sub(r"\s+\(\d+\)$", "", field.strip())
    return ALIASES.get(norm(clean), "internal")


def row_value(row: dict, kind: str) -> str:
    for field, value in row.items():
        if field_kind(field) == kind:
            return value or ""
    return ""


def current_table() -> tuple[str, list[str], list[dict], int]:
    global TABLE_CACHE
    if not SOURCE.exists():
        return "", [], [], 0
    stamp = (str(SOURCE), SOURCE.stat().st_mtime_ns, SOURCE.stat().st_size, SELECTED_SHEET, HEADER_ROW)
    if TABLE_CACHE is None or TABLE_CACHE[0] != stamp:
        TABLE_CACHE = (stamp, read_table(SOURCE, SELECTED_SHEET, HEADER_ROW))
    return TABLE_CACHE[1]


FIELDS = current_table()[1]


def source_fingerprint() -> str:
    if not SOURCE.exists():
        return ""
    digest = hashlib.sha256(SOURCE.read_bytes())
    digest.update(json.dumps({"sheet": SELECTED_SHEET, "header_row": HEADER_ROW,
                              "mapping": FIELD_OVERRIDES, "checks": CHECK_VERSION}, sort_keys=True).encode())
    return digest.hexdigest()


def refresh_source() -> None:
    global SOURCE, FIELDS, TABLE_CACHE
    current = choose_source()
    if not current.exists():
        return
    with JOB_LOCK:
        if JOB["state"] == "running":
            return
        if current != SOURCE:
            SOURCE, TABLE_CACHE = current, None
        FIELDS = current_table()[1]


def select_source(path: Path, sheet: str | None = None, header_row: int = 0) -> None:
    global SOURCE, SELECTED_SHEET, HEADER_ROW, FIELD_OVERRIDES, TABLE_CACHE, FIELDS
    chosen_sheet, headers, _, _ = read_table(path, sheet, header_row)
    SOURCE = path
    SELECTED_SHEET = chosen_sheet if path.suffix.casefold() != ".csv" else None
    HEADER_ROW = header_row
    FIELD_OVERRIDES = {}
    TABLE_CACHE = None
    FIELDS = headers
    SETTINGS.update({"file": file_token(path), "sheet": SELECTED_SHEET,
                     "header_row": HEADER_ROW, "mapping": FIELD_OVERRIDES})
    SELECTION.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")


def set_mapping(mapping: dict[str, str]) -> None:
    global FIELD_OVERRIDES
    allowed = set(ALIASES.values()) | {"internal"}
    if not isinstance(mapping, dict) or any(field not in FIELDS or role not in allowed for field, role in mapping.items()):
        raise ValueError("Mapping contains an unknown column or role")
    FIELD_OVERRIDES = mapping
    SETTINGS["mapping"] = mapping
    SELECTION.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")


def domain(value: str) -> str:
    value = value.strip()
    if "@" in value:
        return value.rsplit("@", 1)[-1].lower()
    parsed = urllib.parse.urlsplit(value if "://" in value else "https://" + value)
    return (parsed.hostname or "").lower().removeprefix("www.")


def valid_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        pass
    parts = value.split("/")
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        return False
    a, b, year = map(int, parts)
    for month, day in ((a, b), (b, a)):
        try:
            datetime(year, month, day)
            return True
        except ValueError:
            pass
    return False


def format_result(field: str, value: str, row: dict) -> tuple[str, str]:
    if missing(value):
        return "missing", "No usable value supplied"
    value = value.strip()
    if value.casefold().startswith("not verified"):
        return "needs_review", "Value itself says it has not been verified"
    kind = field_kind(field)
    if kind == "date":
        return ("format_valid", "Entry date parses, but the original entry log is needed to confirm it; format may be ambiguous") if valid_date(value) else ("invalid", "Entry date cannot be parsed")
    if kind in ("companyemail", "owneremail"):
        return ("format_valid", "Email syntax is valid") if EMAIL_RE.fullmatch(value) else ("invalid", "Invalid email syntax")
    if kind in ("companyphone", "ownerphone"):
        return ("format_valid", "Phone syntax is plausible") if PHONE_RE.fullmatch(value) else ("invalid", "Invalid phone syntax or a non-phone value")
    if kind in ("companywebsite", "linkedincompany", "linkedinowner"):
        parsed = urllib.parse.urlsplit(value if "://" in value else "https://" + value)
        good = parsed.scheme in ("http", "https") and bool(parsed.hostname) and "." in parsed.hostname
        if good and kind.startswith("linkedin") and not domain(value).endswith("linkedin.com"):
            good = False
        return ("format_valid", "URL syntax is valid") if good else ("invalid", "Invalid URL or wrong site for LinkedIn field")
    if kind == "companyemaildomain":
        if not DOMAIN_RE.fullmatch(value):
            return "invalid", "Invalid domain or spreadsheet error"
        email = row_value(row, "companyemail")
        if not missing(email) and EMAIL_RE.fullmatch(email.strip()):
            if domain(email) != value.lower().removeprefix("www."):
                return "conflict", "Does not match the company email domain"
            return "consistent", "Matches the company email domain"
        return "format_valid", "Domain syntax is valid"
    if kind == "zipcode" and row_value(row, "companycountry").strip().upper() in ("USA", "UNITED STATES", "US"):
        return ("format_valid", "US ZIP syntax is valid") if ZIP_RE.fullmatch(value) else ("invalid", "Invalid US ZIP syntax")
    if kind == "internal":
        return "internal_unverified", "Unmapped or internal field; no public source rule is assigned"
    return "present", "Value is present; no independent confirmation yet"


def reference_key(row: dict) -> tuple[str, str, str]:
    name = norm(row_value(row, "companyname"))
    city = norm(row_value(row, "city"))
    state = norm(row_value(row, "state"))
    if city and state:
        return name, city, state
    return name, domain(row_value(row, "companywebsite")), norm(row_value(row, "companycountry"))


def read_source() -> list[dict]:
    return current_table()[2]


def read_reference() -> dict[tuple[str, str, str], list[dict]]:
    if not REFERENCE.exists():
        return {}
    with REFERENCE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    result: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = reference_key(row)
        if all(key):
            result.setdefault(key, []).append(row)
    return result


def read_public_findings() -> dict[tuple[int, str], dict]:
    if not SOURCE.exists() or not PUBLIC_FINDINGS.exists() or hashlib.sha256(SOURCE.read_bytes()).hexdigest() != OLD_SOURCE_HASH:
        return {}
    findings = json.loads(PUBLIC_FINDINGS.read_text(encoding="utf-8"))
    return {(int(item["row_id"]), item["field"]): item for item in findings}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._anchor: list[int] = []
        self.jsonld: list[str] = []
        self._jsonld_parts: list[str] | None = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "address", "li", "section", "footer") and not self.skip:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href", "")
            self.links.append((href, ""))
            self._anchor.append(len(self.links) - 1)
        if tag == "script" and dict(attrs).get("type", "").casefold() == "application/ld+json":
            self._jsonld_parts = []
        if tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("p", "div", "address", "li", "section", "footer") and not self.skip:
            self.parts.append("\n")
        if tag == "a" and self._anchor:
            self._anchor.pop()
        if tag == "script" and self._jsonld_parts is not None:
            self.jsonld.append("".join(self._jsonld_parts))
            self._jsonld_parts = None
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self._jsonld_parts is not None:
            self._jsonld_parts.append(data)
        if not self.skip:
            self.parts.append(data)
            if self._anchor:
                index = self._anchor[-1]
                href, label = self.links[index]
                self.links[index] = (href, label + " " + data)


def public_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        answers = socket.getaddrinfo(parsed.hostname, None)
        return bool(answers) and all(ipaddress.ip_address(item[4][0]).is_global for item in answers)
    except (OSError, ValueError):
        return False


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise urllib.error.URLError("Blocked non-public redirect")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SafeRedirect())


def flatten_jsonld(value):
    if isinstance(value, list):
        for item in value:
            yield from flatten_jsonld(item)
    elif isinstance(value, dict):
        if any(key in value for key in ("@type", "name", "address", "telephone")):
            yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in value:
                yield from flatten_jsonld(value[key])


def fetch_page(url: str) -> tuple[str, str, list[tuple[str, str]], list[dict], str]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not public_url(url):
        return "", "", [], [], "Website URL is not a resolvable public address"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "SalonVerifier/1.0 (+local review)"})
        with OPENER.open(request, timeout=6) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return "", response.geturl(), [], [], "Website did not return text or HTML"
            raw = response.read(300_000)
            charset = response.headers.get_content_charset() or "utf-8"
            parser = TextOnly()
            parser.feed(raw.decode(charset, errors="replace"))
            structured = []
            for block in parser.jsonld:
                try:
                    structured.extend(flatten_jsonld(json.loads(block)))
                except json.JSONDecodeError:
                    continue
            return html.unescape(" ".join(parser.parts)), response.geturl(), parser.links, structured, ""
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        return "", "", [], [], f"Website fetch failed: {type(exc).__name__}"


PAGE_WORDS = ("contact", "location", "about", "team", "staff", "book", "appointment")
BOOKING_WORDS = ("book now", "book online", "book appointment", "schedule online", "appointments", "reserve now")
BOOKING_HOSTS = ("glossgenius.com", "fresha.com", "vagaro.com", "booksy.com", "schedulicity.com")


def fetch_website(url: str, loader=fetch_page) -> dict:
    """Crawl homepage and up to three relevant same-site pages, keeping source URLs."""
    text, final_url, links, structured, error = loader(url)
    if not final_url:
        return {"pages": [], "links": [], "contact_links": [], "structured": [], "error": error}
    pages = [{"url": final_url, "text": text}]
    records = [{"url": final_url, "data": item} for item in structured]
    found_links: list[dict] = []
    contact_links: list[dict] = []
    root_host = domain(final_url)
    candidates = []
    seen = {final_url.rstrip("/")}

    def collect(base: str, page_links: list[tuple[str, str]]):
        for href, label in page_links:
            if href.casefold().startswith(("mailto:", "tel:")):
                contact_links.append({"url": href, "label": " ".join(label.split()), "page": base})
                continue
            target = urllib.parse.urljoin(base, href)
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                continue
            map_host = (parsed.hostname or "").lower()
            map_query = parsed.query if (map_host == "maps.apple.com" or map_host == "google.com"
                                         or map_host.endswith(".google.com")) else ""
            target = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, map_query, ""))
            label = " ".join(label.split())
            found_links.append({"url": target, "label": label, "page": base})
            if domain(target) == root_host and target.rstrip("/") not in seen:
                if any(word in (parsed.path + " " + label).casefold() for word in PAGE_WORDS):
                    candidates.append(target)

    collect(final_url, links)
    priority = lambda target: (0 if "contact" in target.casefold() else
                               1 if any(word in target.casefold() for word in ("about", "team", "staff")) else 2)
    for target in sorted(dict.fromkeys(candidates), key=priority):
        if len(pages) >= 4:
            break
        if target.rstrip("/") in seen:
            continue
        seen.add(target.rstrip("/"))
        sub_text, sub_url, sub_links, sub_structured, _ = loader(target)
        if sub_url and domain(sub_url) == root_host and sub_text:
            pages.append({"url": sub_url, "text": sub_text})
            records.extend({"url": sub_url, "data": item} for item in sub_structured)
            collect(sub_url, sub_links)
    return {"pages": pages, "links": found_links, "contact_links": contact_links,
            "structured": records, "error": error}


def website_fills(row: dict, site: dict) -> dict[str, tuple[str, str]]:
    """Return only unambiguous values from the supplied business website."""
    choices: dict[str, list[tuple[int, str, str]]] = {}

    def offer(kind: str, value, url: str, priority: int):
        if isinstance(value, list):
            for item in value:
                offer(kind, item, url, priority)
            return
        if not isinstance(value, (str, int, float)):
            return
        value = str(value).strip()
        if missing(value):
            return
        if kind in ("companyemail", "owneremail") and not EMAIL_RE.fullmatch(value):
            return
        if kind in ("companyphone", "ownerphone") and not PHONE_RE.fullmatch(value):
            return
        if kind in ("linkedincompany", "linkedinowner"):
            parsed = urllib.parse.urlsplit(value)
            if not parsed.hostname or not domain(value).endswith("linkedin.com"):
                return
        choices.setdefault(kind, []).append((priority, value, url))

    brand = norm(re.split(r"\s+[—–-]\s+", row_value(row, "companyname"), maxsplit=1)[0])
    row_city = norm(row_value(row, "city"))
    for record in site.get("structured", []):
        data = record["data"]
        types = data.get("@type", "")
        if isinstance(types, list):
            types = " ".join(map(str, types))
        if "person" in str(types).casefold():
            title = str(data.get("jobTitle", "")).casefold()
            if any(word in title for word in ("owner", "founder", "proprietor")):
                offer("ownername", data.get("name"), record["url"], 0)
                offer("owneremail", data.get("email"), record["url"], 0)
                offer("ownerphone", data.get("telephone"), record["url"], 0)
                for link in data.get("sameAs", []) if isinstance(data.get("sameAs"), list) else [data.get("sameAs")]:
                    if isinstance(link, str) and "/in/" in link.casefold():
                        offer("linkedinowner", link, record["url"], 0)
            continue
        name = norm(str(data.get("name", "")))
        if not brand or not name or difflib.SequenceMatcher(None, brand, name).ratio() < 0.70:
            continue
        address = data.get("address", {})
        if not isinstance(address, dict):
            address = {}
        locality = norm(str(address.get("addressLocality", "")))
        same_location = not (row_city and locality and row_city != locality)
        offer("companyemail", data.get("email"), record["url"], 0)
        offer("companyphone", data.get("telephone"), record["url"], 0)
        if same_location:
            if isinstance(data.get("address"), str):
                for address_value in addresses(data["address"], city=row_value(row, "city")):
                    offer("companyaddress", address_value, record["url"], 0)
            for kind, key in (("companyaddress", "streetAddress"), ("city", "addressLocality"),
                              ("state", "addressRegion"), ("zipcode", "postalCode"),
                              ("companycountry", "addressCountry")):
                offer(kind, address.get(key), record["url"], 0)
        for link in data.get("sameAs", []) if isinstance(data.get("sameAs"), list) else [data.get("sameAs")]:
            if isinstance(link, str) and "/company/" in link.casefold():
                offer("linkedincompany", link, record["url"], 0)

    for link in site.get("contact_links", []):
        href = link["url"]
        if href.casefold().startswith("mailto:"):
            offer("companyemail", urllib.parse.unquote(href[7:].split("?", 1)[0]), link["page"], 1)
        elif href.casefold().startswith("tel:"):
            offer("companyphone", urllib.parse.unquote(href[4:].split("?", 1)[0]), link["page"], 1)
    for link in site.get("links", []):
        href = link["url"]
        if "/company/" in href.casefold() and domain(href).endswith("linkedin.com"):
            offer("linkedincompany", href, link["page"], 1)
        if "/in/" in href.casefold() and any(word in link["label"].casefold() for word in ("owner", "founder", "proprietor")):
            offer("linkedinowner", href, link["page"], 1)

    if not choices.get("companyemail"):
        for page in site.get("pages", []):
            for email in set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", page["text"], re.I)):
                if domain(email) == domain(page["url"]):
                    offer("companyemail", email, page["url"], 2)
    if not choices.get("companyphone"):
        for page in site.get("pages", []):
            for phone in set(re.findall(r"(?:\+?1[\s().-]*)?\(?\d{3}\)?[\s().-]*\d{3}[\s().-]*\d{4}", page["text"])):
                offer("companyphone", phone, page["url"], 2)

    if not choices.get("companyaddress"):
        for page in site.get("pages", []):
            page_links = [link for link in site.get("links", []) if link.get("page") == page["url"]]
            for address_value in addresses(page["text"], page_links, row_value(row, "city")):
                offer("companyaddress", address_value, page["url"], 1)

    fills = {}
    for kind, candidates in choices.items():
        best = min(item[0] for item in candidates)
        winners = [item for item in candidates if item[0] == best]
        unique = {re.sub(r"\D", "", value) if kind.endswith("phone") else value.casefold(): (value, url)
                  for _, value, url in winners}
        if len(unique) == 1:
            fills[kind] = next(iter(unique.values()))
    return fills


def structured_evidence(field: str, value: str, row: dict, site: dict) -> tuple[str, str, str] | None:
    kind = field_kind(field)
    relevant = {"companyname", "companyemail", "companyphone", "companyaddress",
                "city", "state", "zipcode", "industry"}
    if kind not in relevant:
        return None
    brand = norm(re.split(r"\s+[—–-]\s+", row_value(row, "companyname"), maxsplit=1)[0])
    for record in site.get("structured", []):
        data = record["data"]
        name = norm(str(data.get("name", "")))
        if not brand or not name or difflib.SequenceMatcher(None, brand, name).ratio() < 0.70:
            continue
        address = data.get("address", {})
        if not isinstance(address, dict):
            address = {}
        locality = str(address.get("addressLocality", ""))
        if kind in ("companyaddress", "city", "state", "zipcode"):
            row_city = row_value(row, "city")
            if row_city and locality and norm(row_city) != norm(locality):
                continue
        observed = {
            "companyname": data.get("name", ""),
            "companyemail": data.get("email", ""),
            "companyphone": data.get("telephone", ""),
            "companyaddress": address.get("streetAddress", ""),
            "city": locality,
            "state": address.get("addressRegion", ""),
            "zipcode": address.get("postalCode", ""),
            "industry": data.get("@type", ""),
        }[kind]
        if isinstance(observed, list):
            observed = ", ".join(map(str, observed))
        observed = str(observed).strip()
        if not observed:
            continue
        if kind in ("companyphone", "ownerphone"):
            same = re.sub(r"\D", "", value)[-10:] == re.sub(r"\D", "", observed)[-10:]
        elif kind == "companyname":
            same = difflib.SequenceMatcher(None, brand, norm(observed)).ratio() >= 0.70
        elif kind == "industry":
            same = "salon" in value.casefold() and "salon" in observed.casefold() and "supplier" not in value.casefold()
        else:
            same = norm(value) == norm(observed)
        if same:
            return "structured_match", f"Matches structured business data: {observed[:100]}", record["url"]
        if kind in ("companyaddress", "zipcode", "city", "industry"):
            return "needs_review", f"Structured business data lists {observed[:100]}", record["url"]
    return None


def evidence_for(field: str, value: str, row: dict, site: dict) -> tuple[str, str, str] | None:
    pages = site["pages"]
    links = site["links"]
    kind = field_kind(field)
    if kind == "companywebsite":
        if pages and pages[0]["text"]:
            return "reachable", "Website returned a readable page", pages[0]["url"]
        return "fetch_failed", site["error"] or "Website returned no readable text", ""
    if kind in ("linkedincompany", "linkedinowner"):
        supplied = value.rstrip("/").casefold()
        for link in links:
            if link["url"].rstrip("/").casefold() == supplied:
                return "public_mention", "LinkedIn URL is linked from the supplied company site", link["page"]
        return None
    if kind == "painareas":
        lower = value.casefold()
        if "booking" in lower and ("no " in lower or "needs " in lower):
            for link in links:
                label = link["label"].casefold()
                host = domain(link["url"])
                if any(word in label for word in BOOKING_WORDS) or any(host.endswith(x) for x in BOOKING_HOSTS):
                    return "needs_review", "A booking link exists; review the booking claim in this cell", link["url"]
        return None
    structured = structured_evidence(field, value, row, site)
    if structured:
        return structured
    if kind not in {"companyname", "companyemail", "ownername", "owneremail", "ownerphone",
                    "companyphone", "companyaddress", "city", "state", "zipcode"}:
        return None
    if kind in ("companyphone", "ownerphone"):
        digits = re.sub(r"\D", "", value)
        if len(digits) < 10:
            return None
        for page in pages:
            for phone in re.findall(r"(?:\+?1[\s().-]*)?\(?\d{3}\)?[\s().-]*\d{3}[\s().-]*\d{4}", page["text"]):
                candidate = re.sub(r"\D", "", phone)
                if candidate[-10:] == digits[-10:]:
                    return "public_mention", "Phone number appears on a company website page", page["url"]
        return None
    needle = norm(value)
    if kind == "companyname":
        needle = norm(re.split(r"\s+[—–-]\s+", value, maxsplit=1)[0])
    if len(needle) < 5:
        return None
    for page in pages:
        if needle in norm(page["text"]):
            return "public_mention", "Value appears on a company website page", page["url"]
    return None


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS checks (row_id INTEGER, field TEXT, value TEXT, status TEXT, reason TEXT, evidence TEXT, PRIMARY KEY (row_id, field))")
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return connection


JOB = {"state": "idle", "done": 0, "total": 0, "phase": "", "websites_done": 0,
       "websites_total": 0, "error": ""}
JOB_LOCK = threading.Lock()


def set_job(**values):
    with JOB_LOCK:
        JOB.update(values)


def verify_row(row_id: int, row: dict, refs: dict, website: dict | None):
    key = reference_key(row)
    matches = refs.get(key, []) if all(key) else []
    fills = website_fills(row, website) if website else {}
    results = []
    for field in FIELDS:
        value = row.get(field, "")
        kind = field_kind(field)
        if missing(value) and kind in fills:
            value, evidence = fills[kind]
            results.append((row_id, field, value, "website_filled", "Filled from the company website", evidence))
            continue
        status, reason = format_result(field, value, row)
        evidence = ""
        reference_value = row_value(matches[0], kind).strip() if len(matches) == 1 and kind != "internal" else ""
        if status not in ("missing", "invalid") and reference_value and not missing(reference_value):
            if kind in ("companywebsite", "linkedincompany", "linkedinowner"):
                same = domain(value) == domain(reference_value)
            elif kind in ("companyemail", "owneremail", "companyemaildomain"):
                same = value.strip().casefold() == reference_value.casefold()
            elif kind in ("ownerphone", "companyphone"):
                same = re.sub(r"\D", "", value) == re.sub(r"\D", "", reference_value)
            else:
                same = norm(value) == norm(reference_value)
            status = "reference_match" if same else "reference_conflict"
            reason = "Agrees with independent reference row" if same else "Disagrees with independent reference row"
            evidence = f"reference.csv: matched business row; source value: {reference_value[:160]}"
        elif status not in ("missing", "invalid", "conflict") and website:
            public_result = evidence_for(field, value, row, website)
            if public_result:
                status, reason, evidence = public_result
        results.append((row_id, field, value, status, reason, evidence))
    return results


def run_verification(online: bool):
    try:
        rows = read_source()
        refs = read_reference()
        set_job(state="running", done=0, total=len(rows), phase="websites" if online else "profiles",
                websites_done=0, websites_total=0, error="")
        if online:
            websites = sorted({row_value(r, "companywebsite").strip() for r in rows
                               if not missing(row_value(r, "companywebsite"))})
            set_job(websites_total=len(websites))
            cache = {}
            with ThreadPoolExecutor(max_workers=12) as pool:
                tasks = {pool.submit(fetch_website, url): url for url in websites}
                for count, task in enumerate(as_completed(tasks), 1):
                    cache[tasks[task]] = task.result()
                    set_job(websites_done=count)
        else:
            cache = {}
        set_job(phase="profiles")
        connection = connect()
        connection.execute("DELETE FROM checks")
        batch = []
        for index, row in enumerate(rows, start=1):
            batch.extend(verify_row(index, row, refs, cache.get(row_value(row, "companywebsite").strip())))
            if index % 200 == 0 or index == len(rows):
                connection.executemany("INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?)", batch)
                connection.commit()
                batch.clear()
                set_job(done=index)
        source_hash = source_fingerprint()
        reference_hash = hashlib.sha256(REFERENCE.read_bytes()).hexdigest() if REFERENCE.exists() else ""
        connection.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("source_hash", source_hash), ("reference_hash", reference_hash),
            ("completed_at", datetime.now().isoformat(timespec="seconds")),
            ("online", str(online)),
        ])
        connection.commit()
        connection.close()
        set_job(state="complete")
    except Exception as exc:
        set_job(state="error", error=f"{type(exc).__name__}: {exc}")


class Handler(BaseHTTPRequestHandler):
    def reply(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        refresh_source()
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/files":
            files = []
            for path in available_files():
                try:
                    sheets = [name for name, _ in workbook_sheets(path)]
                    files.append({"token": file_token(path), "name": path.name, "sheets": sheets})
                except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                    files.append({"token": file_token(path), "name": path.name, "sheets": [], "error": "Cannot read workbook"})
            self.reply({"files": files, "selected": file_token(SOURCE) if SOURCE.exists() else "",
                        "sheet": current_table()[0] if SOURCE.exists() else "", "header_row": HEADER_ROW,
                        "detected_header_row": current_table()[3] if SOURCE.exists() else 0})
            return
        if parsed.path == "/api/summary":
            connection = connect()
            counts = dict(connection.execute("SELECT status, COUNT(*) FROM checks GROUP BY status").fetchall())
            reviewed = connection.execute("SELECT COUNT(DISTINCT row_id) FROM checks").fetchone()[0]
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            connection.close()
            findings = read_public_findings()
            with JOB_LOCK:
                job = JOB.copy()
            source_hash = source_fingerprint()
            reference_hash = hashlib.sha256(REFERENCE.read_bytes()).hexdigest() if REFERENCE.exists() else ""
            stale = bool(meta) and (meta.get("source_hash") != source_hash or meta.get("reference_hash") != reference_hash)
            self.reply({"job": job, "rows": len(read_source()), "fields": len(FIELDS),
                        "source_name": SOURCE.name, "sheet_name": current_table()[0],
                        "columns": [{"name": field, "role": field_kind(field)} for field in FIELDS],
                        "reviewed": 0 if stale else reviewed,
                        "counts": {} if stale else counts, "reference_loaded": REFERENCE.exists(), "meta": meta,
                        "public_findings": len(findings), "stale": stale})
            return
        if parsed.path == "/api/rows":
            query = urllib.parse.parse_qs(parsed.query)
            page = max(1, int(query.get("page", ["1"])[0]))
            size = min(100, max(1, int(query.get("size", ["25"])[0])))
            status = query.get("status", [""])[0]
            term = query.get("q", [""])[0].casefold().strip()
            source = read_source()
            findings = read_public_findings()
            connection = connect()
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            stale = meta.get("source_hash") != source_fingerprint()
            if status == "public_conflict":
                ids = {row_id for (row_id, _), finding in findings.items() if finding["verdict"].startswith("conflict")}
            elif status:
                ids = set() if stale else {x[0] for x in connection.execute("SELECT DISTINCT row_id FROM checks WHERE status=?", (status,))}
            else:
                ids = None
            filtered = [(i, row) for i, row in enumerate(source, 1) if (ids is None or i in ids)
                        and (not term or any(term in str(value).casefold() for value in row.values()))]
            selected = filtered[(page - 1) * size:page * size]
            result = []
            for row_id, row in selected:
                checks = [] if stale else [dict(x) for x in connection.execute(
                    "SELECT field, value, status, reason, evidence FROM checks WHERE row_id=? ORDER BY rowid", (row_id,))]
                for check in checks:
                    check["public_finding"] = findings.get((row_id, check["field"]))
                display = row_value(row, "companyname") or next((value for value in row.values() if value.strip()), "(blank row)")
                result.append({"id": row_id, "company": display,
                               "city": row_value(row, "city") or row_value(row, "companycountry"),
                               "state": row_value(row, "state"), "checks": checks})
            connection.close()
            self.reply({"total": len(filtered), "page": page, "size": size, "rows": result})
            return
        if parsed.path == "/api/export":
            connection = connect()
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("source_hash") != source_fingerprint():
                connection.close()
                self.reply({"error": "Run checks on the current CSV before downloading results"}, 409)
                return
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["row_id", "field", "value", "status", "reason", "evidence",
                             "public_verdict", "public_observed", "public_sources", "public_note"])
            findings = read_public_findings()
            for row in connection.execute("SELECT row_id, field, value, status, reason, evidence FROM checks ORDER BY row_id, rowid"):
                finding = findings.get((row["row_id"], row["field"]), {})
                writer.writerow(list(row) + [finding.get("verdict", ""), finding.get("observed", ""),
                                             " | ".join(finding.get("sources", [])), finding.get("note", "")])
            connection.close()
            data = output.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="verification_results.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/export-updated":
            with JOB_LOCK:
                job_state = JOB["state"]
            connection = connect()
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            reference_hash = hashlib.sha256(REFERENCE.read_bytes()).hexdigest() if REFERENCE.exists() else ""
            source_rows = read_source()
            check_count = connection.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
            if (job_state in ("running", "error") or meta.get("source_hash") != source_fingerprint()
                    or meta.get("reference_hash") != reference_hash
                    or check_count != len(source_rows) * len(FIELDS)):
                connection.close()
                self.reply({"error": "Run checks on the current spreadsheet before downloading updated data"}, 409)
                return
            checks = {(row["row_id"], row["field"]): row for row in connection.execute(
                "SELECT row_id, field, value, status, reason, evidence FROM checks")}
            connection.close()
            output = io.StringIO()
            writer = csv.writer(output)
            source_headers = original_csv_headers(SOURCE, HEADER_ROW) if SOURCE.suffix.casefold() == ".csv" else list(FIELDS)
            writer.writerow(source_headers + ["system_verification_status", "system_verification_flags",
                                             "system_verification_checks", "system_verification_evidence",
                                             "system_verified_at"])
            for row_id, source_row in enumerate(source_rows, 1):
                row_checks = [checks[(row_id, field)] for field in FIELDS]
                flags = [f"{check['field']}: {check['status']} ({check['reason']})" for check in row_checks
                         if check["status"] in ("missing", "invalid", "conflict", "reference_conflict",
                                                "needs_review", "fetch_failed")]
                check_summary = [f"{check['field']}: {check['status']}" for check in row_checks]
                evidence = [f"{check['field']}: {check['evidence']}" for check in row_checks if check["evidence"]]
                writer.writerow([checks[(row_id, field)]["value"] if checks[(row_id, field)]["status"] == "website_filled"
                                 else "" if missing(source_row.get(field, "")) else source_row.get(field, "")
                                 for field in FIELDS]
                                + ["Review needed" if flags else "Checked", " | ".join(flags),
                                   " | ".join(check_summary), " | ".join(evidence), meta["completed_at"]])
            data = output.getvalue().encode("utf-8-sig")
            source_name = re.sub(r"[^A-Za-z0-9._-]+", "_", SOURCE.stem).strip("._") or "spreadsheet"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{source_name}_updated.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.reply({"error": "Not found"}, 404)

    def do_POST(self):
        refresh_source()
        if self.path == "/api/select":
            with JOB_LOCK:
                if JOB["state"] == "running":
                    self.reply({"error": "Wait for the current run to finish before changing files"}, 409)
                    return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 10_000:
                    raise ValueError("Selection request is too large")
                choice = json.loads(self.rfile.read(length) or b"{}")
                path = next((item for item in available_files() if file_token(item) == choice.get("file")), None)
                if path is None:
                    raise ValueError("Select a listed spreadsheet file")
                header_row = int(choice.get("header_row", 0) or 0)
                if header_row < -1:
                    raise ValueError("Header row must be -1, zero, or a positive row number")
                select_source(path, choice.get("sheet") or None, header_row)
                self.reply({"selected": file_token(SOURCE), "sheet": current_table()[0],
                            "rows": len(read_source()), "fields": FIELDS})
            except (ValueError, OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
                self.reply({"error": str(exc)}, 400)
            return
        if self.path == "/api/upload":
            with JOB_LOCK:
                if JOB["state"] == "running":
                    self.reply({"error": "Wait for the current run to finish before uploading"}, 409)
                    return
            filename = Path(urllib.parse.unquote(self.headers.get("X-Filename", ""))).name
            if not filename or Path(filename).suffix.casefold() not in (".csv", ".xlsx", ".xlsm"):
                self.reply({"error": "Upload a CSV, XLSX, or XLSM file"}, 400)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 25_000_000:
                self.reply({"error": "File must be between 1 byte and 25 MB"}, 413)
                return
            UPLOADS.mkdir(exist_ok=True)
            path = UPLOADS / filename
            number = 2
            while path.exists():
                path = UPLOADS / f"{Path(filename).stem}_{number}{Path(filename).suffix}"
                number += 1
            path.write_bytes(self.rfile.read(length))
            try:
                select_source(path)
            except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
                path.unlink(missing_ok=True)
                self.reply({"error": f"Cannot read spreadsheet: {exc}"}, 400)
                return
            self.reply({"selected": file_token(SOURCE), "sheet": current_table()[0],
                        "rows": len(read_source()), "fields": FIELDS})
            return
        if self.path == "/api/mapping":
            with JOB_LOCK:
                if JOB["state"] == "running":
                    self.reply({"error": "Wait for the current run to finish before changing column roles"}, 409)
                    return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 20_000:
                    raise ValueError("Mapping request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                set_mapping(payload.get("mapping", {}))
                self.reply({"mapping": {field: field_kind(field) for field in FIELDS}})
            except (ValueError, json.JSONDecodeError) as exc:
                self.reply({"error": str(exc)}, 400)
            return
        if self.path == "/api/run":
            if not SOURCE.exists():
                self.reply({"error": "Choose or upload a spreadsheet first"}, 400)
                return
            with JOB_LOCK:
                running = JOB["state"] == "running"
            if running:
                self.reply({"error": "A verification run is already active"}, 409)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 1000)
            options = json.loads(self.rfile.read(length) or b"{}")
            online = bool(options.get("online"))
            if online and not any(field_kind(field) == "companywebsite" for field in FIELDS):
                self.reply({"error": "Map a website column to Company website before checking public websites"}, 400)
                return
            set_job(state="running", done=0, total=0, phase="starting", error="")
            threading.Thread(target=run_verification, args=(online,), daemon=True).start()
            self.reply({"started": True, "online": online})
            return
        if self.path == "/api/reference":
            if not SOURCE.exists():
                self.reply({"error": "Choose or upload a spreadsheet first"}, 400)
                return
            with JOB_LOCK:
                running = JOB["state"] == "running"
            if running:
                self.reply({"error": "Wait for the current verification run to finish before changing reference data"}, 409)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10_000_000:
                self.reply({"error": "Reference CSV must be under 10 MB"}, 413)
                return
            data = self.rfile.read(length)
            if hashlib.sha256(data).hexdigest() == hashlib.sha256(SOURCE.read_bytes()).hexdigest():
                self.reply({"error": "The source CSV cannot verify itself. Upload an independent reference CSV."}, 400)
                return
            try:
                decoded = data.decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(decoded))
                headers = {field_kind(name) for name in (reader.fieldnames or [])}
                source_rows = read_source()
                if not source_rows:
                    raise ValueError("Selected sheet has no data rows")
                required = {"companyname", "city", "state"} if row_value(source_rows[0], "city") else {"companyname", "companywebsite", "companycountry"}
                if not required.issubset(headers):
                    raise ValueError("Reference needs columns for: " + ", ".join(sorted(required)))
                count = sum(1 for _ in reader)
                if not count:
                    raise ValueError("Reference CSV has no rows")
            except (UnicodeError, csv.Error, ValueError) as exc:
                self.reply({"error": str(exc)}, 400)
                return
            REFERENCE.write_bytes(data)
            self.reply({"loaded": count, "matched_by": " + ".join(sorted(required))})
            return
        self.reply({"error": "Not found"}, 404)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Local spreadsheet verification dashboard")
    cli.add_argument("--port", type=int, default=8770, help="Local HTTP port (default: 8770)")
    arguments = cli.parse_args()
    with connect():
        pass
    print(f"Salon Verifier: http://127.0.0.1:{arguments.port}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler).serve_forever()
    except OSError as exc:
        raise SystemExit(f"Could not start on port {arguments.port}: {exc}. Try --port 8771.") from exc
