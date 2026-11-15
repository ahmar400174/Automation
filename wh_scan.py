"""
WinningHunter weekly scan: finds fashion dropshipping competitors.
"""
import argparse, datetime as dt, json, os, re, sys, time, unicodedata
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from scoring import (clean_domain, is_active_row, is_real_site, is_report_candidate, is_generic_host,
                     score_brand)
from verify_active import (CACHE_FILE, STATUS_FAILED, STATUS_INACTIVE, STATUS_STALE, STATUS_UNVERIFIED,
                           STATUS_VERIFIED, ad_is_live, cache_key, resolve_pages, save_cache,
                           verification_summary)
from verify_active import adlib_url as _meta_adlib_url

BASE = os.environ.get("WH_BASE_URL", "https://app.winninghunter.com").rstrip("/")
KEY = os.environ.get("WH_API_KEY", "")
ENDPOINT = os.environ.get("WH_ENDPOINT", "/api/v1/adlibrary")
AUTH_STYLE = os.environ.get("WH_AUTH", "x-api-key")
MIN_INTERVAL = float(os.environ.get("WH_MIN_INTERVAL", "1.1"))

PROMO = re.compile(r"(?i)\b(sale|sales|clearance|outlet|deals?|store|stores|shop|shops|official|mall|"
                   r"soldes|saldi|rebajas|promo|discount|wholesale|factory)\b")
NOT_FASHION = re.compile(r"(?i)\b(jewel\w*|diamonds?|rings?|hair|wigs?|beauty|skin\w*|pets?|dogs?|cats?|puppy|pillows?|"
                         r"health|journal|sleep|tools?|garden|kitchen|furniture|toys?|baby|tech|gadgets?)\b")


def clean_letters(name):
    for ch in name:
        if ch in " &'’":
            continue
        if not ch.isalpha():
            return False
        uname = unicodedata.name(ch, "")
        if not uname.startswith("LATIN") or "MATHEMATICAL" in uname:
            return False
    return True


def name_ok(name):
    n = (name or "").strip()
    if len(n) < 4 or not clean_letters(n):
        return False
    if PROMO.search(n):
        return False
    return True


def looks_fashion(name, domain):
    return not NOT_FASHION.search(name or "") and not NOT_FASHION.search(domain or "")


def domain_of(ad):
    for k in ("urlStore", "product_url", "link"):
        u = ad.get(k)
        if u:
            try:
                h = urlparse(u).netloc.lower()
                if h:
                    return h
            except Exception:
                pass
    return ad.get("shopify_shopifydomain") or ""


API_COUNT_FIELDS = ("total_active_ads_on_page", "active_ads_count", "page_active_ads")
AD_ID_FIELDS = ("ad_archive_id", "adArchiveID", "adArchiveId", "ad_id", "adId")
MAX_AD_IDS = 5000


def ad_uid(ad):
    for field in AD_ID_FIELDS:
        value = ad.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def adlib_link(page_id, country):
    """The link written in the report. It is the SAME url the verifier opens on Meta, so the number in
    the report is what you see when you click the link."""
    return _meta_adlib_url(page_id, country, active=True)


COUNTRY_FIELDS = ("countries", "target_countries", "targeting_countries",
                  "delivery_countries", "available_countries")


def ad_targets_country(ad, country):
    if not country:
        return True
    cc = str(country).upper()
    for field in COUNTRY_FIELDS:
        raw = ad.get(field)
        if not raw:
            continue
        if isinstance(raw, str):
            values = {part.strip().upper() for part in raw.split(",") if part.strip()}
        elif isinstance(raw, (list, tuple, set)):
            values = {str(part).strip().upper() for part in raw if str(part).strip()}
        else:
            continue
        if not values:
            continue
        return cc in values or "ALL" in values or "ROW" in values
    return True


def page_claim_count(ad):
    for field in API_COUNT_FIELDS:
        value = ad.get(field)
        if value in (None, ""):
            continue
        try:
            count = int(float(value))
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count, field
    return None, ""


_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
_last = [0.0]
RATE_FILE = os.environ.get("WH_RATE_FILE", "")


def wait_turn():
    if RATE_FILE:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl:
            with open(RATE_FILE, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                txt = f.read().strip()
                last = float(txt) if txt else 0.0
                wait = MIN_INTERVAL - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
                f.seek(0)
                f.truncate()
                f.write(str(time.time()))
                f.flush()
                fcntl.flock(f, fcntl.LOCK_UN)
            return
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


# ---------------------------------------------------------------------------------------------------
# History of competitors already reported in EARLIER weeks ("new competitors only" feature).
#   * Persisted in the file named by WH_SEEN_FILE (run_weekly.py points it to results/seen_before.csv,
#     which is committed to the repository so it survives between runs). Empty = feature disabled.
#   * Format: key,first_seen_period   (key = a domain, or "pid:<page id>")
#   * Entries first seen in the CURRENT period never filter that same period, so re-running or resuming
#     a week can never empty it.
#   * The history is only WRITTEN after a country scan finished and was exported (never on a partial run).
#   * It is loaded once, before the scan: ads of the same page inside one run are never dropped.
# ---------------------------------------------------------------------------------------------------
SEEN_HEADER = "key,first_seen_period"
SEEN = set()


def seen_file():
    return (os.environ.get("WH_SEEN_FILE") or "").strip()


def load_seen_before(period=None, path=None):
    """Keys reported in earlier periods (entries of `period` itself are ignored)."""
    path = path if path is not None else seen_file()
    seen = set()
    if not path or not os.path.exists(path):
        return seen
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                key, _, first = line.partition(",")
                key, first = key.strip().lower(), first.strip()
                if not key or first.lower() == "first_seen_period":      # blank line or header row
                    continue
                if key and (period is None or first != str(period)):
                    seen.add(key)
    except OSError as exc:
        print(f"WARNING: could not read the seen history: {exc}")
    return seen


def record_seen(state, period, path=None):
    """Append the competitors of a COMPLETED scan to the history (existing keys are kept untouched)."""
    path = path if path is not None else seen_file()
    if not path:
        return 0
    try:
        existing = set()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    key = line.split(",", 1)[0].strip().lower()
                    if key and key not in ("key", "domain"):
                        existing.add(key)
        fresh = []
        for brand in state.get("brands", {}).values():
            for key in (clean_domain(brand.get("domain")), "pid:" + str(brand.get("page_id") or "")):
                key = (key or "").strip().lower()
                if key and key != "pid:" and key not in existing:
                    existing.add(key)
                    fresh.append(key)
        if not fresh and os.path.exists(path):
            return 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8") as handle:
            if new_file:
                handle.write(SEEN_HEADER + "\n")
            for key in fresh:
                handle.write(f"{key},{period}\n")
        return len(fresh)
    except OSError as exc:
        print(f"WARNING: could not update the seen history: {exc}")
        return 0


# ---- per-ad details for the expandable report rows (Library ID, launch date, landing page, preview) ----
START_FIELDS = ("start_date", "startDate", "ad_delivery_start_time", "delivery_start_time", "start_time",
                "ad_creation_time", "creation_time", "created_at", "createdAt", "first_seen", "firstSeen",
                "first_seen_at", "launch_date", "launched_at", "started_at", "begin_date", "date")
LANDING_FIELDS = ("product_url", "link", "link_url", "landing_page", "landing_url", "cta_link", "urlStore")
THUMB_FIELDS = ("image", "poster", "facebook_thumbnail_url", "facebook_image_url", "thumbnail",
                "video_preview_image_url", "preview_image")
MAX_AD_ROWS = 60


def launch_date(ad):
    """The date the ad started running as YYYY-MM-DD, or '' when the record carries none."""
    for field in START_FIELDS:
        value = ad.get(field)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
                number = float(value)
                if number > 1e11:           # milliseconds
                    number /= 1000.0
                if number > 1e8:
                    return dt.datetime.fromtimestamp(number, dt.timezone.utc).date().isoformat()
                continue
            text = str(value).strip().replace("Z", "+00:00")
            return dt.datetime.fromisoformat(text[:19] if "T" in text or " " in text else text[:10]).date().isoformat()
        except (ValueError, OverflowError, OSError):
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
                try:
                    return dt.datetime.strptime(str(value).strip()[:10], fmt).date().isoformat()
                except ValueError:
                    continue
    return ""


def ad_row(ad, uid):
    url = ""
    for field in LANDING_FIELDS:
        value = ad.get(field)
        if isinstance(value, str) and value.strip():
            url = value.strip()
            break
    thumb = ""
    for field in THUMB_FIELDS:
        value = ad.get(field)
        if isinstance(value, str) and value.strip().startswith("http"):
            thumb = value.strip()
            break
    return {"id": uid, "d": launch_date(ad), "u": url[:300], "i": thumb[:500]}


# ---- ADDED: Library ID extraction (later definition of ad_row below is the one used) ----
ID_KEYS = ("ad_archive_id", "adArchiveID", "adArchiveId", "ad_library_id", "adLibraryId", "library_id", "libraryId",
           "archive_id", "archiveId", "fb_ad_id", "facebook_ad_id", "ad_id", "adId", "id", "_id")
LIB_URL_RE = re.compile(r"ads/library/?\?[^\"'\s]*?\bid=(\d{10,20})")


def library_id(ad, uid=""):
    """Meta Library ID of the ad. Meta ids are plain 10-20 digit numbers, so an internal id (hex, uuid, short number)
    is never mistaken for one. Also read from any Ad-Library link the record carries."""
    for value in (uid,) + tuple(ad.get(k) for k in ID_KEYS):
        text = str(value or "").strip()
        if text.isdigit() and 10 <= len(text) <= 20:
            return text
    for value in ad.values():
        if isinstance(value, str) and "ads/library" in value:
            match = LIB_URL_RE.search(value)
            if match:
                return match.group(1)
    return ""


_ad_row_basic = ad_row


def ad_row(ad, uid):
    """Same record as before, with the Library ID found in more places."""
    row = _ad_row_basic(ad, uid)
    row["id"] = library_id(ad, uid) or row.get("id") or ""
    return row



def add_ads(state, ads, country=None):
    """Groups the ads of one API call by Facebook page."""
    scraped = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    for ad in ads:
        pid = str(ad.get("page_id") or "").strip()
        if not pid:
            continue

        dom = clean_domain(domain_of(ad))

        if SEEN and ("pid:" + pid.lower() in SEEN or (dom and dom in SEEN)):
            continue

        if is_generic_host(domain_of(ad)):
            continue

        if not ad_targets_country(ad, country):
            continue

        live, flag = ad_is_live(ad)
        if live is False:
            continue

        text = (ad.get("copy") or "")[:400].replace("\n", " ")
        b = state["brands"].setdefault(pid, {
            "page_id": pid, "name": ad.get("pageName", ""), "domain": dom,
            "raw_records_count": 0, "unique_ads_count": 0, "missing_id_records": 0,
            "duplicate_records": 0, "ad_level_live_records": 0, "ad_ids": [],
            "api_reported_count": 0, "verified_active_ads_count": None,
            "verified_active_ads_count_approximate": False,
            "verification_status": STATUS_UNVERIFIED,
            "first_scraped_at": scraped, "last_scraped_at": scraped,
            "copy": text[:200], "copies": []})
        b.setdefault("ad_ids", [])
        b.setdefault("duplicate_records", 0)
        b.setdefault("missing_id_records", 0)
        b.setdefault("ad_level_live_records", 0)
        b["last_scraped_at"] = scraped
        b["raw_records_count"] = int(b.get("raw_records_count") or 0) + 1

        uid = ad_uid(ad)
        if uid:
            if uid in b["ad_ids"]:
                b["duplicate_records"] += 1
                continue
            if len(b["ad_ids"]) < MAX_AD_IDS:
                b["ad_ids"].append(uid)
        else:
            b["missing_id_records"] += 1
        row = ad_row(ad, uid)
        rows_kept = b.setdefault("ad_rows", [])
        if len(rows_kept) < MAX_AD_ROWS and (row["id"] or row["d"] or row["u"] or row["i"]):
            signature = (row["id"], row["d"], row["u"], row["i"])
            if signature not in {(r.get("id"), r.get("d"), r.get("u"), r.get("i")) for r in rows_kept}:
                rows_kept.append(row)
            elif not row["id"]:
                rows_kept.append(row)      # ADDED: identical ads without an ID are separate ads of one creative group
        keys_seen = state.setdefault("ad_keys", [])          # which fields the API really sends (diagnostic)
        if len(keys_seen) < 80:
            for key in ad:
                if key not in keys_seen and len(keys_seen) < 80:
                    keys_seen.append(key)
        b["unique_ads_count"] = int(b.get("unique_ads_count") or 0) + 1
        if live is True:
            b["ad_level_live_records"] += 1

        claim, _ = page_claim_count(ad)
        if claim is not None and claim >= (b.get("api_reported_count") or 0):
            b["api_reported_count"] = claim

        if text and text not in b.setdefault("copies", []) and len(b["copies"]) < 3:
            b["copies"].append(text)
        if not b["domain"]:
            b["domain"] = dom
        if not b.get("thumb"):
            b["thumb"] = (ad.get("image") or ad.get("poster") or ad.get("facebook_thumbnail_url")
                          or ad.get("facebook_image_url") or "")


def call(params):
    headers = {"Authorization": f"Bearer {KEY}"} if AUTH_STYLE == "bearer" else {"x-api-key": KEY}
    for attempt in range(6):
        wait_turn()
        try:
            r = _SESSION.get(BASE + ENDPOINT, params=params, headers=headers, timeout=120)
        except requests.RequestException as e:
            print("network error, retry:", e)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            print("HTTP", r.status_code, "retry in", 10 * (attempt + 1), "s")
            time.sleep(10 * (attempt + 1))
            continue
        print("HTTP", r.status_code, r.text[:500])
        return None
    print("too many failures, stopping (progress is saved)")
    return None


def build_params(a, scroll):
    one_year_ago = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 365 * 86400))
    p = {
        "countries": a.country, "niches": "CG", "website": "SH", "limit": 50, "page": 0,
        "from": a.date_from, "to": a.date_to,
        "pagefrom": a.page_from or one_year_ago, "pageto": a.page_to or a.date_to,
    }
    if a.language:
        p["languages"] = a.language
    if scroll:
        p["scroll"] = scroll
    return p


def extract(resp):
    if isinstance(resp, dict):
        ads = resp.get("data") or resp.get("ads") or resp.get("results") or []
        nxt = resp.get("scroll")
        if not nxt and isinstance(resp.get("pagination"), dict):
            nxt = resp["pagination"].get("scroll_next") or resp["pagination"].get("scroll")
        return ads, nxt
    return resp, None


KNOWN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known")
SKIP_HOSTS = re.compile(r"(facebook|fb\.com|google|instagram|youtube|tiktok|pinterest|whatsapp|bit\.ly|canva|drive)")


def norm_domain(d):
    return clean_domain(d)


def load_known():
    domains, pids = set(), set()
    here = os.path.dirname(os.path.abspath(__file__))
    files = []
    if os.path.isdir(KNOWN_DIR):
        files += [os.path.join(KNOWN_DIR, f) for f in os.listdir(KNOWN_DIR)
                  if f.lower().endswith(".csv") and f != "seen_before.csv"]
    files += [os.path.join(here, f) for f in os.listdir(here)
              if f.lower().startswith("known_") and f.lower().endswith(".csv")]
    for fn in files:
        txt = open(fn, encoding="utf-8-sig", errors="replace").read()
        for m in re.finditer(r"https?://([^/\s,\"'?&]+)", txt):
            h = norm_domain(m.group(1))
            if "." in h and not SKIP_HOSTS.search(h):
                domains.add(h)
        for m in re.finditer(r"[?&]q=([A-Za-z0-9.-]+\.[A-Za-z]{2,})", txt):
            domains.add(norm_domain(m.group(1)))
        pids.update(re.findall(r"view_all_page_id=(\d+)", txt))
    return domains, pids


def known_label(domain, pids, known):
    kd, kp = known
    if (domain and domain in kd) or any(p in kp for p in pids):
        return "In your Sheet"
    return ""


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_state(state, period=None):
    if period and state.get("period") and state["period"] != period:
        print(f"WARNING: saved progress is for {state['period']}, not {period}: ignored (fresh state).")
        state = {"scroll": None, "calls": 0, "total": None, "done": False, "brands": {}}
    if period:
        state["period"] = period
    for b in state.get("brands", {}).values():
        claim = b.get("api_reported_count")
        for legacy in ("page_total", "active_ads", "active_on_page"):
            if claim is None:
                claim = b.get(legacy)
        b["api_reported_count"] = max(0, _int(claim))
        raw = b.get("raw_records_count")
        b["raw_records_count"] = max(0, _int(raw if raw is not None else b.get("ads")))
        unique = b.get("unique_ads_count")
        if unique is None:
            unique = len(b.get("ad_ids") or []) or b["raw_records_count"]
        b["unique_ads_count"] = max(0, _int(unique))
        verified = b.get("verified_active_ads_count")
        b["verified_active_ads_count"] = _int(verified) if verified not in (None, "") else None
        b["verified_active_ads_count_approximate"] = bool(b.get("verified_active_ads_count_approximate"))
        b["verification_status"] = b.get("verification_status") or STATUS_UNVERIFIED
        for legacy in ("ads", "active_ads", "live_ads", "checked_ads", "active_source",
                       "page_total", "active_on_page"):
            b.pop(legacy, None)
        b.setdefault("ad_ids", [])
        b.setdefault("ad_rows", [])
        b.setdefault("duplicate_records", 0)
        b.setdefault("missing_id_records", 0)
        b.setdefault("ad_level_live_records", 0)
    return state


HEAD = ["Ads Library link", "Site link", "Page name", "Score", "Verified active ads", "Verification",
        "Verification checked", "API reported ads", "Raw records", "Unique ads", "Type", "Known",
        "Other pages of this site", "Why this score", "Ad text (start)", "Preview URL"]
WIDTHS = (90, 40, 32, 7, 16, 18, 20, 15, 12, 10, 10, 16, 40, 70, 80, 30)


def group_by_site(rows):
    groups = {}
    for r in rows:
        b = r[2]
        key = norm_domain(b["domain"]) or "pid:" + b["page_id"]
        groups.setdefault(key, []).append(r)
    out = []
    for key, g in groups.items():
        shown = max(g, key=lambda r: (r[2].get("verification_status") in (STATUS_VERIFIED, STATUS_INACTIVE),
                                      r[2].get("verified_active_ads_count") or 0, r[0], r[1]))
        scored = max(g, key=lambda r: (r[0], r[1]))
        others = sorted({r[2]["name"] for r in g if r[2]["name"] != shown[2]["name"]})
        out.append({"score": scored[0], "ads": sum(r[1] for r in g),
                    "active": shown[2].get("verified_active_ads_count"),
                    "approximate": bool(shown[2].get("verified_active_ads_count_approximate")),
                    "status": shown[2].get("verification_status") or STATUS_UNVERIFIED,
                    "b": shown[2], "cat": scored[3],
                    "why": scored[4], "others": others, "domain": "" if key.startswith("pid:") else key,
                    "pids": [r[2]["page_id"] for r in g],
                    "thumb": shown[2].get("thumb") or next((r[2].get("thumb") for r in g if r[2].get("thumb")), "")})
    return out


def sanitize_sites(sites):
    kept, by_page = [], {}
    for r in sorted(sites, key=lambda x: (-_int(x.get("active")), -x.get("score", 0), -x.get("ads", 0))):
        status = str(r.get("status") or "").strip().lower()
        if not is_report_candidate(r["domain"], status):
            continue
        if status == STATUS_VERIFIED and not is_active_row(r["domain"], r.get("active"), status):
            continue
        page_id = str(r["b"].get("page_id") or "")
        if page_id and page_id in by_page:
            continue
        if page_id:
            by_page[page_id] = True
        kept.append(r)
    return kept


def listed_page_ids(state):
    """Page ids of the stores that CAN appear in the report: their site is a real competitor domain.

    A page without a store site (or with a generic platform link such as facebook.com / linktr.ee) is dropped
    by sanitize_sites whatever Meta says, so asking Meta about it only burns the rate-limit budget. The
    domain does not depend on verification, which is what makes this list safe to compute BEFORE it.
    WH_VERIFY_SCOPE=all restores the old behaviour (every scraped page is checked).
    """
    if os.environ.get("WH_VERIFY_SCOPE", "all").strip().lower() == "all":
        return {str(b.get("page_id")) for b in state["brands"].values() if b.get("page_id")}
    return {str(b.get("page_id")) for b in state["brands"].values()
            if b.get("page_id") and is_real_site(norm_domain(b.get("domain")))}


def verification_order(state, country, page_id):
    """Sort key: Top-tab stores first (highest score first), then the biggest advertisers.

    With a limited Meta budget (rate limit / time) the pages that matter most are verified first.
    """
    brand = state["brands"].get(page_id) or {}
    try:
        score = score_brand(brand, country)[0]
    except Exception:
        score = 0
    return (-score, -(brand.get("api_reported_count") or 0))


def resolve_state_verification(state, country, a, live=False, progress_callback=None):
    pages = [(b.get("page_id"), country) for b in state["brands"].values() if b.get("page_id")]
    max_age = getattr(a, "verify_max_age", None)
    resolved = resolve_pages(pages, cache_path=CACHE_FILE, max_age_hours=max_age)
    if live:
        pending = []
        listed = listed_page_ids(state)
        for key, entry in resolved.items():
            if entry["status"] in (STATUS_UNVERIFIED, STATUS_FAILED, STATUS_STALE) \
                    and str(entry["page_id"]) in listed:
                brand = state["brands"].get(entry["page_id"]) or {}
                pending.append((entry["page_id"], entry["country"], brand.get("name") or ""))
        pending.sort(key=lambda t: verification_order(state, country, t[0]))
        # UPDATED LOGIC (resume where the last run stopped): pages that were NEVER asked go first; pages that
        # already came back "unknown" go last, oldest attempt first. Before, the same ~80 unanswerable pages sat
        # at the front of the queue in every run and used up the whole rate-limit budget before any new page.
        _checked = {str(e["page_id"]): (e.get("checked") or "") for e in resolved.values()}
        pending.sort(key=lambda t: (1 if _checked.get(str(t[0])) else 0,
                                    _checked.get(str(t[0])) or "",
                                    verification_order(state, country, t[0])))
        _fresh = sum(1 for t in pending if not _checked.get(str(t[0])))
        print(f"Queue order: {_fresh} never-asked page(s) first, {len(pending) - _fresh} earlier-unanswered page(s) last.", flush=True)
        skipped = len([e for e in resolved.values()
                       if e["status"] in (STATUS_UNVERIFIED, STATUS_FAILED, STATUS_STALE)]) - len(pending)
        if skipped > 0:
            print(f"{skipped} page(s) have no store site: they cannot appear in the report, so Meta is not "
                  f"asked about them.", flush=True)
        # 0 / empty = verify EVERY page (exact Meta count for every store); a number caps it.
        limit = int(os.environ.get("WH_VERIFY_LIMIT", "0") or 0)
        pending = pending[:limit] if limit else pending
        # Time budget: this country gets an equal share of what is left of the run (never less than 1 min).
        now = time.time()
        global_deadline = float(os.environ.get("WH_DEADLINE", "0") or 0)
        left = max(1, int(os.environ.get("WH_COUNTRIES_LEFT", "1") or 1))
        # Equal share of what is left. When the budget is already used up the country is STILL exported (its
        # workbook is built from the cache) but Meta is not asked: the old "never less than 1 min" rule made the
        # first countries overrun the round and the last ones (GB, NL) were skipped entirely.
        if global_deadline:
            share = (global_deadline - now) / left
            vdeadline = now + share if share >= 20 else now
        else:
            vdeadline = None
        if pending and vdeadline is not None and vdeadline - now < 20:
            print(f"{len(pending)} page(s) wait for the next round: this round's browser time is used up.",
                  flush=True)
            pending = []
        if pending:
            try:
                from verify_active import verify_targets
                print(f"Verifying {len(pending)} page(s) live on Meta…", flush=True)
                # refresh=True: these pages have no FRESH answer (never asked / failed / older than the
                # freshness window), so an old cached number must never be reused for them.
                verify_targets(pending, workers=int(os.environ.get("WH_VERIFY_WORKERS", "3") or 3),
                               cache_path=CACHE_FILE, refresh=True, deadline=vdeadline,
                               progress_callback=progress_callback)
                resolved = resolve_pages(pages, cache_path=CACHE_FILE, max_age_hours=max_age)
            except ImportError:
                print("Playwright is not installed: using the cached verification only.")
            except Exception as exc:
                print(f"Live verification stopped ({type(exc).__name__}: {exc}): cached answers kept.")
    for page_id, brand in state["brands"].items():
        entry = resolved.get(cache_key(page_id, country)) or {}
        brand["verified_active_ads_count"] = entry.get("verified")
        brand["verified_active_ads_count_approximate"] = bool(entry.get("approximate"))
        brand["verification_status"] = entry.get("status") or STATUS_UNVERIFIED
        brand["verification_checked"] = entry.get("checked") or ""
    return resolved


def export(state, a, path, verify_live=None, progress_callback=None):
    import openpyxl
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def clean(v):
        if not isinstance(v, str):
            return v
        value = ILLEGAL_CHARACTERS_RE.sub("", v)
        if value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    wb = openpyxl.Workbook()
    top = wb.active
    if top is None:
        top = wb.create_sheet("Top")
    else:
        top.title = "Top"
    other = wb.create_sheet("Others")
    for ws in (top, other):
        ws.append(HEAD)
        for col, w in zip("ABCDEFGHIJKLMNOP", WIDTHS):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A2"
    live = a.verify_live if verify_live is None else bool(verify_live)
    resolved = resolve_state_verification(state, a.country, a, live=live,
                                          progress_callback=progress_callback)
    rows = []
    for b in state["brands"].values():
        score, cat, why = score_brand(b, a.country)
        rows.append((score, b["unique_ads_count"], b, cat, why))
    grouped = group_by_site(rows)
    sites = sanitize_sites(grouped)
    dropped = len(grouped) - len(sites)
    known = load_known()
    n_top = n_known = 0
    for r in sites:
        b = r["b"]
        label = known_label(r["domain"], r["pids"], known)
        n_known += bool(label)
        ws = top if r["score"] >= a.top_score else other
        n_top += ws is top
        ws.append([clean(x) for x in [
            adlib_link(b["page_id"], a.country),
            ("https://" + clean_domain(b["domain"])) if b["domain"] else "",
            b["name"], r["score"],
            (f"~{r['active']}" if r.get("approximate") and r["active"] is not None else r["active"]),
            r["status"], b.get("verification_checked") or "",
            b["api_reported_count"], b["raw_records_count"], b["unique_ads_count"],
            r["cat"], label, ", ".join(r["others"]), "; ".join(r["why"]),
            " | ".join(b.get("copies") or [b.get("copy", "")])[:300], r.get("thumb", "")]])
    # Pages that cannot appear in the report (no store site) were deliberately not asked on Meta: they must not
    # count as "unverified" in the header, and must never trigger the automatic extra verification rounds.
    listed = listed_page_ids(state)
    summary = verification_summary({k: v for k, v in resolved.items() if str(v.get("page_id")) in listed})
    audit = wb.create_sheet("Meta")
    audit.append(["key", "value"])
    for key, value in (
            ("country", a.country), ("from", a.date_from), ("to", a.date_to),
            ("pages", len(state["brands"])),
            ("pages_without_store_site", len(state["brands"]) - len(listed)),
            ("raw_records", sum(_int(b.get("raw_records_count")) for b in state["brands"].values())),
            ("unique_ads", sum(_int(b.get("unique_ads_count")) for b in state["brands"].values())),
            ("dedupe", 1 if any(b.get("ad_ids") for b in state["brands"].values()) else 0),
            ("duplicate_records", sum(_int(b.get("duplicate_records")) for b in state["brands"].values())),
            ("missing_id_records", sum(_int(b.get("missing_id_records")) for b in state["brands"].values())),
            ("scraped_at", min((b.get("first_scraped_at") for b in state["brands"].values()
                                if b.get("first_scraped_at")), default="")),
            ("verified", summary.get(STATUS_VERIFIED, 0)),
            ("inactive", summary.get(STATUS_INACTIVE, 0)),
            ("unverified", summary.get(STATUS_UNVERIFIED, 0)),
            ("stale", summary.get(STATUS_STALE, 0)),
            ("failed", summary.get(STATUS_FAILED, 0)),
            ("generated_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            ("source", "WinningHunter API (scan) + Meta Ad Library (verification)")):
        audit.append([key, value])
    path = os.fspath(path)
    tmp_path = path + ".tmp.xlsx"
    wb.save(tmp_path)
    os.replace(tmp_path, path)
    print(f"Excel saved: {path}")


def _progress_exporter(state, args, path):
    """Write an atomic per-country workbook every ten percentage points of the live check."""
    last_saved = {"percent": 0}

    def checkpoint(done, total, cache):
        if total <= 0:
            return
        percent = min(100, int(done * 100 / total))
        boundary = (percent // 10) * 10
        if boundary < 10 or boundary <= last_saved["percent"]:
            return
        last_saved["percent"] = boundary
        # Persist the verifier's in-memory answers before exporting from the cache.
        save_cache(cache, CACHE_FILE)
        export(state, args, path, verify_live=False)
        print(f"COUNTRY PROGRESS CHECKPOINT: {args.country} {boundary}% ({done}/{total})", flush=True)

    return checkpoint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--language")
    ap.add_argument("--page-from"); ap.add_argument("--page-to")
    ap.add_argument("--max-calls", type=int, default=5000)
    ap.add_argument("--top-score", type=int, default=5, help="minimum score for the Top tab")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--allow-partial", action="store_true",
                    help="when the scan stops early, still export what was scanned and exit with code 3")
    ap.add_argument("--verify-live", action="store_true",
                    default=os.environ.get("WH_VERIFY_LIVE", "").strip().lower() in ("1", "true", "yes"))
    ap.add_argument("--verify-max-age", type=float, default=None)
    a = ap.parse_args()

    tag = f"{a.country}_{a.date_from}_{a.date_to}"
    sfile, xfile = f"scan_{tag}.json", f"competitors_{tag}.xlsx"
    state = json.load(open(sfile, encoding="utf-8")) if os.path.exists(sfile) else \
        {"scroll": None, "calls": 0, "total": None, "done": False, "brands": {}}
    state = normalize_state(state, tag)

    if a.export_only:
        progress = _progress_exporter(state, a, xfile) if a.verify_live else None
        export(state, a, xfile, progress_callback=progress)
        return
    if not KEY:
        print("Set WH_API_KEY first."); sys.exit(1)

    global SEEN
    SEEN = load_seen_before(period=a.date_from)
    if SEEN:
        print(f"Seen history: {len(SEEN)} competitor key(s) from earlier weeks will be skipped.", flush=True)

    if a.estimate:
        p = build_params(a, None)
        p["limit"] = 1
        resp = call(p)
        total = resp.get("total") if isinstance(resp, dict) else None
        if total is None:
            print("ESTIMATE|%s|unknown|unknown" % a.country)
            sys.exit(1)
        calls = -(-int(total) // 50)
        print("ESTIMATE|%s|%d|%d" % (a.country, int(total), calls))
        return

    if a.probe:
        resp = call(build_params(a, None))
        ads, nxt = extract(resp)
        print("probe done")
        return

    n = 0
    deadline = float(os.environ.get("WH_DEADLINE", "0") or 0)
    stop_reason = f"the max-calls limit ({a.max_calls}) was reached"
    while n < a.max_calls and not state["done"]:
        if deadline and time.time() > deadline:
            stop_reason = "the time budget ran out"
            break
        resp = call(build_params(a, state["scroll"]))
        if resp is None:
            stop_reason = "the API stopped answering (error or too many failures)"
            break
        ads, nxt = extract(resp)
        state["calls"] += 1; n += 1
        if state["total"] is None and isinstance(resp, dict):
            state["total"] = resp.get("total")
        add_ads(state, ads, a.country)
        state["scroll"] = nxt
        if not nxt or len(ads) == 0:
            state["done"] = True
        if n % 25 == 0 or state["done"]:
            json.dump(state, open(sfile, "w", encoding="utf-8"))
    json.dump(state, open(sfile, "w", encoding="utf-8"))
    print(f"API calls this run: {n} of at most {a.max_calls}; "
          f"{'all results were read' if state['done'] else 'stopped because ' + stop_reason}. "
          f"(about {MIN_INTERVAL:g}s between calls, so {int(60 / max(MIN_INTERVAL, 0.1))} calls per minute at most)", flush=True)
    if not state["done"]:
        print(f"Scan incomplete after {state['calls']} call(s) (stopped by max-calls, the time limit or an API error).")
        if a.allow_partial and state["brands"]:
            print("WARNING: exporting the PARTIAL result (progress is kept, history is not updated).")
            progress = _progress_exporter(state, a, xfile) if a.verify_live else None
            export(state, a, xfile, progress_callback=progress)
            sys.exit(3)
        sys.exit(2)
    progress = _progress_exporter(state, a, xfile) if a.verify_live else None
    export(state, a, xfile, progress_callback=progress)
    added = record_seen(state, a.date_from)
    if added:
        print(f"Seen history updated: {added} new key(s).")
    print("DONE")


if __name__ == "__main__":
    main()
