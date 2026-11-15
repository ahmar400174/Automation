"""
Regression tests for the weekly scan pipeline.

No live API and no live Facebook access is needed: the WinningHunter client, the verification cache and
the per-country workbooks are all deterministic fixtures. Run from the project root:

    python -m pytest tests -q
"""
# pyright: reportMissingImports=false
import argparse
import asyncio
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_weekly  # noqa: E402
import scoring  # noqa: E402
import verify_active  # noqa: E402
import wh_scan  # noqa: E402

NOW = dt.datetime(2026, 10, 28, 12, 0, 0)


def fresh_stamp(hours_ago=1):
    """An ISO time that is always inside the freshness window, whatever day the suite runs on.

    The old fixtures hard-coded 2026-10-28: once the 72 h window passed, every test that resolves a cache
    without an explicit `now` silently started failing.
    """
    return (dt.datetime.now() - dt.timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ fixtures / helpers
def ad(page_id="111", **fields):
    base = {"page_id": page_id, "pageName": "Maison Test", "urlStore": "https://maison-test.fr",
            "copy": "Robe en lin, collection automne", "ad_archive_id": f"a{page_id}",
            "is_active": True}
    base.update(fields)
    return base


def state():
    return {"scroll": None, "calls": 0, "total": None, "done": False, "brands": {}}


def brand_of(state_dict, page_id="111"):
    return state_dict["brands"][page_id]


def write_cache(path, entries):
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def cache_entry(live, state_name="live", checked=None, error=None, confirmed=None):
    entry = {"live": live, "state": state_name, "checked": checked or fresh_stamp(), "error": error,
             "page_id": "111", "country": "FR"}
    # A zero is only trusted when it was double-checked (verify_active.check_page sets this flag).
    if confirmed or (confirmed is None and live == 0 and state_name == "inactive"):
        entry["confirmed"] = True
    return entry


def workbook_row(page_id="111", site="maison-test.fr", name="Maison Test", score=7, verified: int | None = 5,
                 status="verified", checked=None, claim=6, raw=4, unique=3,
                 category="fashion", known="", others="", why="clothing words", text="Robe en lin",
                 thumb="", country="FR"):
    """One row in the exact column order of wh_scan.HEAD (the per-country workbook)."""
    checked = checked or fresh_stamp()
    return [wh_scan.adlib_link(page_id, country), "https://" + site, name, score, verified, status,
            checked, claim, raw, unique, category, known, others, why, text, thumb]


def write_country_workbook(path, rows, top_rows=None, other_rows=None):
    wb = openpyxl.Workbook()
    top = wb.active
    assert top is not None
    top.title = "Top"
    top.append(wh_scan.HEAD)
    for row in (rows if top_rows is None else top_rows):
        top.append(row)
    other = wb.create_sheet("Others")
    other.append(wh_scan.HEAD)
    for row in (other_rows or []):
        other.append(row)
    wb.save(path)
    return path


def merge_fixture(tmp_path, rows, start="2026-10-20", end="2026-10-26", run=None):
    work = tmp_path / "work"
    results = tmp_path / "results"
    work.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    write_country_workbook(work / f"competitors_FR_{start}_{end}.xlsx", rows)
    base_run = {"execution_id": "run-test-1", "started_at": "2026-10-28T09:00:00",
                "from": start, "to": end, "countries": ["FR"]}
    base_run.update(run or {})
    _, _, outcome = run_weekly.merge(["FR"], start, end, work, results, base_run)
    return work, results, outcome


def html_payload(path):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"var DATA = (\{.*?\});", text, re.S)
    assert match is not None
    data = json.loads(match.group(1))
    return text, data


def test_incremental_country_snapshot_is_saved_and_marks_remaining_countries_partial(tmp_path):
    start, end = "2026-10-20", "2026-10-26"
    work, results = tmp_path / "work", tmp_path / "results"
    work.mkdir(); results.mkdir()
    write_country_workbook(work / f"competitors_US_{start}_{end}.xlsx",
                           [workbook_row(page_id="901", country="US", site="checkpoint.example")])
    run_weekly.begin_incremental_run({"execution_id": "checkpoint-test", "from": start, "to": end,
                                      "countries": ["US", "FR"], "mode": "verify",
                                      "work": str(work), "results": str(results)})
    try:
        run_weekly.checkpoint_country("US", 0)
        manifest = json.loads((results / "run_manifest.json").read_text(encoding="utf-8"))
        assert (results / f"competitors_week_{start}.html").is_file()
        assert (results / f"competitors_week_{start}.xlsx").is_file()
        assert manifest["countries_completed"] == ["US"]
        assert manifest["partial_countries"] == ["FR"]
        assert manifest["outcome"] == "incomplete"
    finally:
        run_weekly.begin_incremental_run({})


def test_progress_snapshot_is_saved_without_trusted_answers_but_canonical_is_preserved(tmp_path):
    start, end = "2026-10-20", "2026-10-26"
    work, results = tmp_path / "work", tmp_path / "results"
    work.mkdir(); results.mkdir()
    write_country_workbook(work / f"competitors_US_{start}_{end}.xlsx", [
        workbook_row(page_id="902", country="US", site="pending.example", verified=None,
                     status="unverified")])
    run_weekly.begin_incremental_run({"execution_id": "no-meta-yet", "from": start, "to": end,
                                      "countries": ["US"], "mode": "verify",
                                      "work": str(work), "results": str(results)})
    try:
        run_weekly.checkpoint_country("US", 3)
        live_dir = results / "live_runs" / "no-meta-yet"
        assert (live_dir / f"competitors_week_{start}.html").is_file()
        assert (live_dir / f"competitors_week_{start}.xlsx").is_file()
        assert not (results / f"competitors_week_{start}.html").exists()
        manifest = json.loads((live_dir / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["outcome"] == "incomplete" and manifest["verification"] == "partial"
    finally:
        run_weekly.begin_incremental_run({})


def test_progress_exporter_saves_once_per_ten_percent_boundary(monkeypatch, tmp_path, capsys):
    saves, exports = [], []
    monkeypatch.setattr(wh_scan, "save_cache", lambda cache, path: saves.append((dict(cache), path)))
    monkeypatch.setattr(wh_scan, "export", lambda state, args, path, **kwargs: exports.append((path, kwargs)))
    args = type("Args", (), {"country": "US"})()
    callback = wh_scan._progress_exporter({}, args, str(tmp_path / "country.xlsx"))
    callback(1, 10, {"US:1": {"live": 2}})
    callback(2, 10, {"US:1": {"live": 2}, "US:2": {"live": 3}})
    callback(2, 10, {"US:1": {"live": 2}, "US:2": {"live": 3}})
    assert len(saves) == len(exports) == 2
    assert all(kwargs == {"verify_live": False} for _, kwargs in exports)
    output = capsys.readouterr().out
    assert "US 10%" in output and "US 20%" in output


def test_verify_targets_deduplicates_same_page_and_country_before_cache_lookup(tmp_path):
    cache_path = tmp_path / "cache.json"
    write_cache(cache_path, {"US:901": {"live": 4, "state": "live", "checked": fresh_stamp()},
                             "FR:902": {"live": 2, "state": "live", "checked": fresh_stamp()}})
    cache = verify_active.verify_targets([("901", "US", "First"), ("901", "us", "Duplicate"),
                                          ("902", "FR", "Other")], cache_path=cache_path)
    assert sorted(cache) == ["FR:902", "US:901"]


# ------------------------------------------------------------------ 1-2 URL scope
def test_global_country_filter_uses_all():
    url = run_weekly.adlib_url("123", "")
    assert "country=ALL" in url
    assert "active_status=active" in url and "ad_type=all" in url
    assert "view_all_page_id=123" in url
    assert "country=IT" not in url


def test_country_specific_configuration_is_preserved():
    url = run_weekly.adlib_url("42", "it")
    assert "country=IT" in url and "country=ALL" not in url
    assert "country=FR" in wh_scan.adlib_link("42", "FR")
    assert "country=ALL" in wh_scan.adlib_link("42", "")


# ------------------------------------------------------------------ 3-4 counts
def test_verified_inactive_page_stays_in_the_output_with_zero(tmp_path):
    """A claim of 6 with a verified zero publishes the row as 0 (kept, not dropped)."""
    rows = [workbook_row(page_id="111", site="maison-test.fr", verified=0, status="inactive", claim=6)]
    _, results, run = merge_fixture(tmp_path, rows)
    assert run["outcome"] == "complete"
    assert run["reported_competitors"] == 1
    assert run["verified_active_ads"] == 0            # the row is visible, its count is an honest 0
    text, data = html_payload(results / "competitors_week_2026-10-20.html")
    assert len(data["top"]) == 1
    assert data["top"][0]["n"] == "Maison Test"
    assert data["top"][0]["c"] == 0                   # displayed as 0, not dropped
    assert "Maison Test" in text


def test_verified_active_page_keeps_its_actual_count(tmp_path):
    rows = [workbook_row(page_id="111", site="one.fr", name="Live One", verified=5),
            workbook_row(page_id="222", site="two.fr", name="Dead Two", verified=0,
                         status="inactive", claim=9)]
    _, results, run = merge_fixture(tmp_path, rows)
    _, data = html_payload(results / "competitors_week_2026-10-20.html")
    counts = {row["n"]: row["c"] for row in data["top"]}
    assert counts == {"Live One": 5, "Dead Two": 0}   # active page keeps its count, inactive shows 0
    assert run["verified_active_ads"] == 5            # only verified counts are summed
    assert run["reported_competitors"] == 2


def test_unverified_stale_and_failed_rows_are_listed_without_a_number(tmp_path):
    """Every real competitor is listed. Only the verified page carries a number; the rest show none."""
    rows = [workbook_row(page_id="111", site="live.fr", name="Live Brand", verified=5),
            workbook_row(page_id="222", site="unverified.fr", name="Unknown Brand",
                         verified=None, status="unverified"),
            workbook_row(page_id="333", site="failed.fr", name="Broke Brand",
                         verified=None, status="verification_failed"),
            workbook_row(page_id="444", site="stale.fr", name="Old Brand",
                         verified=None, status="stale")]
    _, results, run = merge_fixture(tmp_path, rows)
    text, data = html_payload(results / "competitors_week_2026-10-20.html")
    counts = {row["n"]: row["c"] for row in data["top"]}
    assert counts == {"Live Brand": 5, "Unknown Brand": None, "Broke Brand": None, "Old Brand": None}
    status = {row["n"]: row["v"] for row in data["top"]}
    assert status["Unknown Brand"] == "unverified" and status["Old Brand"] == "stale"
    assert run["reported_competitors"] == 4          # all competitors are listed
    assert run["verified_competitors"] == 1          # one of them has a real Meta answer
    assert run["verified_active_ads"] == 5           # unknown counts are never summed


def test_wh_scan_export_keeps_the_inactive_page_with_zero(tmp_path, monkeypatch):
    """The per-country workbook (wh_scan.sanitize_sites) keeps the inactive row, count 0."""
    cache = write_cache(tmp_path / "cache.json", {
        "FR:111": cache_entry(0, "inactive"),           # Meta shows no running ad
        "FR:222": cache_entry(7),                        # Meta shows 7 running ads
    })
    monkeypatch.setattr(wh_scan, "CACHE_FILE", cache)
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111", urlStore="https://dead-brand.fr"),
                           ad(page_id="222", urlStore="https://live-brand.fr")], "FR")
    args = argparse.Namespace(country="FR", date_from="2026-10-20", date_to="2026-10-26",
                              top_score=5, verify_live=False, verify_max_age=None)
    path = tmp_path / "country.xlsx"
    wh_scan.export(data, args, str(path))
    wb = openpyxl.load_workbook(path)
    body = []
    for sheet in ("Top", "Others"):
        if sheet in wb.sheetnames:
            body += list(wb[sheet].iter_rows(values_only=True))
    head = {name: i for i, name in enumerate(body[0])}
    by_site = {str(r[head["Site link"]]): r for r in body[1:] if r[head["Site link"]]}
    assert "https://dead-brand.fr" in by_site          # the inactive page is still there
    assert by_site["https://dead-brand.fr"][head["Verified active ads"]] == 0
    assert by_site["https://dead-brand.fr"][head["Verification"]] == "inactive"
    assert by_site["https://live-brand.fr"][head["Verified active ads"]] == 7   # untouched


def test_verified_active_and_inactive_candidates_are_reported(tmp_path):
    """Verified counts, authoritative zeros AND not-yet-verified competitors are all in the report."""
    rows = [
        workbook_row(page_id="111", site="live.fr", name="Live Brand", verified=5, status="verified"),
        workbook_row(page_id="222", site="inactive.fr", name="Inactive Brand", verified=0,
                     status="inactive"),
        workbook_row(page_id="333", site="unverified.fr", name="Unverified Brand",
                     verified=None, status="unverified"),
        workbook_row(page_id="444", site="stale.fr", name="Stale Brand", verified=None, status="stale"),
        workbook_row(page_id="555", site="failed.fr", name="Failed Brand", verified=None,
                     status="verification_failed"),
    ]
    _, results, run = merge_fixture(tmp_path, rows)
    assert run["outcome"] == "complete"                # every country was scanned
    assert run["verification"] == "partial"            # but 3 pages have no verified number yet
    assert run["reported_competitors"] == 5
    assert run["verified_competitors"] == 2
    assert run["verified_active_ads"] == 5             # only verified counts are summed
    text, data = html_payload(results / "competitors_week_2026-10-20.html")
    counts = {row["n"]: row["c"] for row in data["top"]}
    assert counts == {"Live Brand": 5, "Inactive Brand": 0, "Unverified Brand": None,
                      "Stale Brand": None, "Failed Brand": None}
    for domain in ("inactive.fr", "unverified.fr", "stale.fr", "failed.fr"):
        assert domain in text
    # Facebook URLs are intentionally present as the Ads Library verification links;
    # generic platform domains must not be used as competitor Site links.
    assert all(row["s"].split("/")[2] not in ("facebook.com", "instagram.com")
               for row in data["top"] if row.get("s"))


def test_inactive_row_is_kept_with_zero_in_the_excel_output(tmp_path):
    rows = [workbook_row(page_id="111", site="dead.fr", name="Dead Brand", verified=0,
                         status="inactive", claim=4),
            workbook_row(page_id="222", site="live.fr", name="Live Brand", verified=7,
                         status="verified", claim=7)]
    work, results, run = merge_fixture(tmp_path, rows)
    import openpyxl as xl
    wb = xl.load_workbook(results / "competitors_week_2026-10-20.xlsx")
    sheet = wb["All Top"]
    body = list(sheet.iter_rows(values_only=True))
    head = {name: i for i, name in enumerate(body[0])}
    by_site = {str(r[head["Site link"]]): r for r in body[1:]}
    assert "https://dead.fr" in by_site
    assert by_site["https://dead.fr"][head["Verified active ads"]] == 0     # kept, shown as 0
    assert by_site["https://dead.fr"][head["Verification"]] == "inactive"
    assert by_site["https://live.fr"][head["Verified active ads"]] == 7     # verified count untouched
    assert run["reported_competitors"] == 2


def test_unverified_stale_and_failed_only_rows_do_not_publish(tmp_path):
    rows = [
        workbook_row(page_id="111", site="unverified.fr", name="Unverified Brand",
                     verified=None, status="unverified"),
        workbook_row(page_id="222", site="failed.fr", name="Failed Brand",
                     verified=None, status="verification_failed"),
        workbook_row(page_id="333", site="stale.fr", name="Stale Brand",
                     verified=None, status="stale"),
    ]
    _, results, run = merge_fixture(tmp_path, rows)
    assert run["outcome"] == "empty"
    assert run["reported_competitors"] == 0
    assert not (results / "competitors_week_2026-10-20.html").exists()
    assert not (results / "competitors_week_2026-10-20.xlsx").exists()


def test_a_run_of_only_unverified_pages_does_not_publish_a_report(tmp_path):
    """Unknown verification outcomes cannot be presented as a trustworthy report."""
    rows = [workbook_row(page_id="111", site="unverified.fr", verified=None, status="unverified"),
            workbook_row(page_id="222", site="failed.fr", verified=None, status="verification_failed"),
            workbook_row(page_id="333", site="stale.fr", verified=None, status="stale")]
    work, results, run = merge_fixture(tmp_path, rows)
    assert not (results / "competitors_week_2026-10-20.html").exists()
    assert not (results / "competitors_week_2026-10-20.xlsx").exists()
    assert run["outcome"] == "empty"
    assert run["reported_competitors"] == 0
    assert run["verified_active_ads"] == 0
    assert run["verified_active_ads"] == 0


def test_zero_records_produce_no_report(tmp_path):
    work, results, run = merge_fixture(tmp_path, [])
    assert run["outcome"] == "empty"
    assert "No ads returned" in run["note"]
    assert not (results / "competitors_week_2026-10-20.html").exists()


# ------------------------------------------------------------------ 5-7 dedupe
def test_duplicate_ad_ids_do_not_inflate_the_unique_count():
    data = state()
    first = ad()
    wh_scan.add_ads(data, [first, dict(first)], "FR")
    brand = brand_of(data)
    assert brand["raw_records_count"] == 2
    assert brand["unique_ads_count"] == 1
    assert brand["duplicate_records"] == 1
    assert "active_ads" not in brand and "live_ads" not in brand


def test_distinct_ads_stay_distinct():
    data = state()
    wh_scan.add_ads(data, [ad(ad_archive_id="x1"), ad(ad_archive_id="x2")], "FR")
    brand = brand_of(data)
    assert brand["raw_records_count"] == 2
    assert brand["unique_ads_count"] == 2


def test_missing_identifiers_are_handled_explicitly():
    data = state()
    without_id = ad()
    without_id.pop("ad_archive_id")
    wh_scan.add_ads(data, [without_id, dict(without_id)], "FR")
    brand = brand_of(data)
    assert brand["unique_ads_count"] == 2          # never silently collapsed into one ad
    assert brand["missing_id_records"] == 2


# ------------------------------------------------------------------ 8-9 status
def test_explicitly_inactive_ads_are_excluded():
    data = state()
    wh_scan.add_ads(data, [ad(is_active=False)], "FR")
    assert data["brands"] == {}


def test_past_end_date_excludes_the_ad_even_with_a_true_flag():
    data = state()
    wh_scan.add_ads(data, [ad(stop_date="2026-01-01")], "FR")
    assert data["brands"] == {}


def test_ambiguous_status_is_unverified_and_row_is_excluded(tmp_path):
    data = state()
    bare = ad()
    for field in ("is_active", "status"):
        bare.pop(field, None)
    wh_scan.add_ads(data, [bare], "FR")
    brand = brand_of(data)
    assert brand["raw_records_count"] == 1
    assert brand["ad_level_live_records"] == 0

    cache = write_cache(tmp_path / "cache.json", {})
    resolved = verify_active.resolve_pages([("111", "FR")], cache_path=cache, now=NOW)
    entry = resolved["FR:111"]
    assert entry["verified"] is None
    assert entry["status"] == verify_active.STATUS_UNVERIFIED
    assert scoring.is_active_row("maison-test.fr", entry["verified"], entry["status"]) is False


def test_gate_keeps_only_verified_and_inactive_rows_and_refuses_broken_links():
    assert scoring.is_active_row("maison-test.fr", 5, "verified") is True      # active page, real count
    assert scoring.is_active_row("maison-test.fr", 0, "inactive") is True       # verified inactive KEPT
    assert scoring.is_active_row("maison-test.fr", 0, "verified") is False      # zero must be authoritative
    assert scoring.is_active_row("maison-test.fr", 6, "inactive") is True       # caller normalizes it to 0
    assert scoring.is_active_row("maison-test.fr", None, "unverified") is False
    assert scoring.is_active_row("maison-test.fr", None, "stale") is False
    assert scoring.is_active_row("maison-test.fr", None, "verification_failed") is False
    # a generic platform link never gets a row, even with a perfect status
    assert scoring.is_active_row("facebook.com", 0, "inactive") is False
    assert scoring.is_active_row("m.instagram.com", 12, "verified") is False


# ------------------------------------------------------------------ 10-12 verification
def test_api_verification_succeeds(tmp_path):
    cache = write_cache(tmp_path / "cache.json", {"FR:111": cache_entry(7)})
    resolved = verify_active.resolve_pages([("111", "FR")], cache_path=cache, now=NOW)
    assert resolved["FR:111"]["verified"] == 7
    assert resolved["FR:111"]["status"] == verify_active.STATUS_VERIFIED
    assert scoring.is_active_row("maison-test.fr", 7, "verified") is True


def test_verification_failure_is_not_a_zero_and_row_is_excluded(tmp_path):
    failed = write_cache(tmp_path / "failed.json",
                         {"FR:111": cache_entry(None, "error", error="TimeoutError: timed out")})
    entry = verify_active.resolve_pages([("111", "FR")], cache_path=failed, now=NOW)["FR:111"]
    assert entry["verified"] is None
    assert entry["status"] == verify_active.STATUS_FAILED
    # A failure is not a verified count or authoritative zero, so it cannot enter the report.
    assert scoring.is_active_row("maison-test.fr", entry["verified"], entry["status"]) is False

    blocked = write_cache(tmp_path / "blocked.json",
                          {"FR:111": cache_entry(None, "unknown", error="rate limited")})
    entry = verify_active.resolve_pages([("111", "FR")], cache_path=blocked, now=NOW)["FR:111"]
    assert entry["status"] == verify_active.STATUS_UNVERIFIED


def test_stale_verification_is_excluded_from_verified_totals(tmp_path):
    cache = write_cache(tmp_path / "cache.json", {"FR:111": cache_entry(9, checked="2026-10-01T00:00:00")})
    entry = verify_active.resolve_pages([("111", "FR")], cache_path=cache, now=NOW)["FR:111"]
    assert entry["status"] == verify_active.STATUS_STALE
    assert entry["verified"] is None
    assert scoring.is_active_row("maison-test.fr", entry["verified"], entry["status"]) is False


def test_rate_limits_and_transient_errors_use_bounded_retries():
    assert verify_active.should_retry("error", "TimeoutError", 0, 2) is True
    assert verify_active.should_retry("unknown", "no counter", 1, 2) is True
    assert verify_active.should_retry("error", "TimeoutError", 2, 2) is False   # bounded
    assert verify_active.should_retry("unknown", verify_active.RATE_LIMITED, 0, 2) is False
    assert verify_active.retry_delay(1) == 2
    assert verify_active.retry_delay(9) == 8                                    # capped


# ------------------------------------------------------------------ 13 pagination
def test_incomplete_pagination_cannot_produce_a_complete_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WH_API_KEY", "test-key")
    monkeypatch.setattr(wh_scan, "KEY", "test-key")
    monkeypatch.setattr(wh_scan, "call", lambda params: {"data": [ad()], "scroll": "next-page"})
    monkeypatch.setattr(sys, "argv", ["wh_scan.py", "--country", "FR",
                                      "--from", "2026-10-20", "--to", "2026-10-26", "--max-calls", "1"])
    with pytest.raises(SystemExit) as exc:
        wh_scan.main()
    assert exc.value.code == 2
    assert not (tmp_path / "competitors_FR_2026-10-20_2026-10-26.xlsx").exists()
    assert (tmp_path / "scan_FR_2026-10-20_2026-10-26.json").exists()          # progress is kept


# ------------------------------------------------------------------ 14 fresh artifacts
def test_previous_run_data_never_appears_in_a_new_run(tmp_path):
    old_week = workbook_row(name="OldBrand", site="old-brand.fr")
    work = tmp_path / "work"
    work.mkdir()
    write_country_workbook(work / "competitors_FR_2026-10-13_2026-10-19.xlsx", [old_week])

    work, results, run = merge_fixture(tmp_path, [workbook_row(name="NewBrand", site="new-brand.fr")])
    text, data = html_payload(results / "competitors_week_2026-10-20.html")
    assert "NewBrand" in text
    assert "OldBrand" not in text
    assert len(data["top"]) == 1


def test_fresh_workdir_purges_previous_workbooks_and_keeps_progress(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "competitors_FR_2026-10-13_2026-10-19.xlsx").write_bytes(b"stale")
    (work / "scan_FR_2026-10-13_2026-10-19.json").write_text('{"brands": {}}', encoding="utf-8")
    monkeypatch_store = tmp_path / "results" / "scan_data"
    import run_weekly as rw
    original = rw.STORE
    rw.STORE = monkeypatch_store
    try:
        rw.fresh_workdir(work)
    finally:
        rw.STORE = original
    assert work.is_dir()
    assert not (work / "competitors_FR_2026-10-13_2026-10-19.xlsx").exists()
    assert (monkeypatch_store / "scan_FR_2026-10-13_2026-10-19.json.gz").exists()


def test_resolve_archived_period_falls_back_to_latest_shared_period(tmp_path):
    store = tmp_path / "scan_data"
    store.mkdir()
    for filename in (
        "scan_ES_2026-10-26_2026-11-01.json.gz",
        "scan_ES_2026-11-03_2026-11-04.json.gz",
        "scan_US_2026-10-26_2026-11-01.json.gz",
        "scan_US_2026-11-03_2026-11-04.json.gz",
    ):
        (store / filename).write_bytes(b"fixture")
    assert run_weekly.resolve_archived_period(["ES", "US"], "2026-11-05", "2026-11-05", store) == (
        "2026-11-03", "2026-11-04")
    assert run_weekly.resolve_archived_period(["ES", "US"], "2026-10-26", "2026-11-01", store) == (
        "2026-10-26", "2026-11-01")
    assert run_weekly.resolve_archived_period(["ES", "FR"], "2026-11-05", "2026-11-05", store) is None


# ------------------------------------------------------------------ 15-16 HTML
def test_html_reports_timestamps_source_and_scope(tmp_path):
    work, results, run = merge_fixture(tmp_path, [workbook_row()])
    text, data = html_payload(results / "competitors_week_2026-10-20.html")
    assert "run-test-1" in text
    assert "WinningHunter API" in text and "Meta Ad Library" in text
    assert "2026-10-20 to 2026-10-26" in text
    assert "FR" in text
    assert re.search(r"2026-\d\d-\d\dT\d\d:\d\d:\d\d", text)      # ISO 8601
    assert data["meta"]["outcome"] == "complete"


def test_html_counts_agree_with_verified_records(tmp_path):
    rows = [workbook_row(page_id="111", site="one.fr", verified=5),
            workbook_row(page_id="222", site="two.fr", verified=12)]
    _, results, run = merge_fixture(tmp_path, rows)
    _, data = html_payload(results / "competitors_week_2026-10-20.html")
    numbers = sorted(row["c"] for row in data["top"])
    assert numbers == [5, 12]
    assert run["verified_active_ads"] == 17
    assert sum(1 for _ in data["top"]) == run["reported_competitors"]


def _report_row(checked, active=5):
    cells = ["link", "site", "name", 7, active, "verified", checked] + [""] * 8
    return {"key": "x.fr", "score": 7, "active": active, "status": "verified", "ads": 1,
            "cells": cells, "img": ""}


def test_freshness_is_computed_from_the_check_time():
    run = {"execution_id": "e", "from": "a", "to": "b", "countries": ["FR"]}
    scan = {"pages": 1, "dedupe": 1}
    now = dt.datetime.now().replace(microsecond=0)
    assert run_weekly.describe_run(run, [_report_row(now.isoformat())], {}, scan)["freshness"] == "fresh"
    old = (now - dt.timedelta(hours=100)).isoformat()
    assert run_weekly.describe_run(run, [_report_row(old)], {}, scan)["freshness"] == "stale"


def test_validate_report_rejects_unverified_rows_and_accepts_verified_or_inactive():
    unverified = {"key": "x.fr", "active": 0, "status": "unverified",
                  "cells": ["link", "site", "name", 7, 0, "unverified"] + [""] * 10}
    stale = {"key": "y.fr", "active": 0, "status": "stale",
             "cells": ["link", "site", "name", 7, 0, "stale"] + [""] * 10}
    failed = {"key": "z.fr", "active": 0, "status": "verification_failed",
              "cells": ["link", "site", "name", 7, 0, "verification_failed"] + [""] * 10}
    inactive = {"key": "dead.fr", "active": 0, "status": "inactive",
                "cells": ["link", "site", "name", 7, 0, "inactive"] + [""] * 10}
    assert run_weekly.validate_report([unverified, stale, failed, inactive])
    assert run_weekly.validate_report([inactive]) == []
    bad_inactive = dict(inactive, active=9)
    assert run_weekly.validate_report([bad_inactive])
    good_verified = {"key": "a.fr", "active": 5, "status": "verified",
                      "cells": ["link", "site", "name", 7, 5, "verified"] + [""] * 10}
    assert run_weekly.validate_report([good_verified]) == []


# ------------------------------------------------------------------ artifact freshness
def test_successful_run_replaces_the_previous_report_and_manifest(tmp_path, monkeypatch):
    """A later successful run must refresh THIS week's artifacts, not leave stale output behind."""
    start, end = "2026-10-20", "2026-10-26"
    work, results = tmp_path / "work", tmp_path / "results"
    work.mkdir()
    results.mkdir()
    write_country_workbook(work / f"competitors_FR_{start}_{end}.xlsx",
                           [workbook_row(name="Fresh Brand", site="fresh.fr", verified=4)])
    stale_html = results / f"competitors_week_{start}.html"
    stale_html.write_text("STALE REPORT FROM AN EARLIER RUN", encoding="utf-8")
    stale_manifest = results / "run_manifest.json"
    stale_manifest.write_text('{"execution_id": "old-run"}', encoding="utf-8")

    excel, _, run = run_weekly.merge(
        ["FR"], start, end, work, results,
        {"execution_id": "run-new-1", "from": start, "to": end, "countries": ["FR"]})
    assert excel is not None
    text = stale_html.read_text(encoding="utf-8")
    assert "STALE REPORT FROM AN EARLIER RUN" not in text   # old artifact replaced, not left behind
    assert "Fresh Brand" in text                              # latest scan data published
    assert "run-new-1" in text                                # new execution id visible

    # the manifest of the latest run overwrites the stale one and points at the fresh report
    monkeypatch.setattr(run_weekly, "RESULTS", results)
    monkeypatch.setattr(run_weekly, "MANIFEST", results / "run_manifest.json")
    run["html_path"] = f"results/competitors_week_{start}.html"
    run_weekly.write_manifest(run)
    saved = json.loads(stale_manifest.read_text(encoding="utf-8"))
    assert saved["execution_id"] == "run-new-1"
    assert saved["html_path"].endswith(f"competitors_week_{start}.html")


# ------------------------------------------------------------------ 17 no partial overwrite
def test_no_candidates_does_not_overwrite_a_valid_latest_report(tmp_path):
    """An unverified-only run cannot replace the last trustworthy report."""
    work = tmp_path / "work"
    results = tmp_path / "results"
    work.mkdir()
    results.mkdir()
    write_country_workbook(work / "competitors_FR_2026-10-20_2026-10-26.xlsx",
                           [workbook_row(site="unverified.fr", verified=None, status="unverified")])
    latest = results / "competitors_week_2026-10-20.html"
    latest.write_text("SENTINEL LATEST REPORT", encoding="utf-8")
    excel_path, summary, run = run_weekly.merge(
        ["FR"], "2026-10-20", "2026-10-26", work, results,
        {"execution_id": "run-test-2", "from": "2026-10-20", "to": "2026-10-26", "countries": ["FR"]})
    assert excel_path is None
    assert run["outcome"] == "empty"
    assert latest.read_text(encoding="utf-8") == "SENTINEL LATEST REPORT"


def test_truly_empty_run_does_not_overwrite_a_valid_latest_report(tmp_path):
    """A run with no candidate pages at all keeps the old sentinel untouched."""
    work = tmp_path / "work"
    results = tmp_path / "results"
    work.mkdir()
    results.mkdir()
    # empty workbook: headers only, no candidate rows at all
    wb = openpyxl.Workbook()
    active = wb.active
    assert active is not None
    active.title = "Top"
    active.append(wh_scan.HEAD)
    wb.save(work / "competitors_FR_2026-10-20_2026-10-26.xlsx")
    latest = results / "competitors_week_2026-10-20.html"
    latest.write_text("SENTINEL LATEST REPORT", encoding="utf-8")
    excel_path, summary, run = run_weekly.merge(
        ["FR"], "2026-10-20", "2026-10-26", work, results,
        {"execution_id": "run-test-3", "from": "2026-10-20", "to": "2026-10-26", "countries": ["FR"]})
    assert excel_path is None
    assert run["outcome"] == "empty"
    assert latest.read_text(encoding="utf-8") == "SENTINEL LATEST REPORT"


# ------------------------------------------------------------------ 18 credentials
def test_missing_credentials_fail_clearly_without_exposing_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WH_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_weekly.py", "--countries", "US", "--max-calls", "1"])
    with pytest.raises(SystemExit) as exc:
        run_weekly.main()
    message = str(exc.value.code)
    assert "WH_API_KEY" in message
    assert "test-key" not in message


# ------------------------------------------------------------------ 19 existing behaviour
def test_existing_scoring_and_url_helpers_still_work():
    assert scoring.clean_domain("https://www.maison-test.fr/shop?x=1") == "maison-test.fr"
    assert scoring.is_generic_host("m.facebook.com") is True
    assert scoring.is_generic_host("maison-test.fr") is False
    assert scoring.is_real_site("maison-test.fr") is True
    assert scoring.is_real_site("myshop") is False
    assert wh_scan.name_ok("Maison Test") is True
    assert wh_scan.name_ok("SALE 50%") is False
    assert wh_scan.adlib_link("9", "FR").endswith("view_all_page_id=9")


def test_verification_summary_counts_statuses(tmp_path):
    cache = write_cache(tmp_path / "cache.json", {
        "FR:1": cache_entry(4),
        "FR:2": cache_entry(0, "inactive"),
        "FR:3": cache_entry(None, "unknown", error="rate limited"),
        "FR:4": cache_entry(None, "error", error="TimeoutError"),
    })
    resolved = verify_active.resolve_pages([("1", "FR"), ("2", "FR"), ("3", "FR"), ("4", "FR")],
                                           cache_path=cache, now=NOW)
    summary = verify_active.verification_summary(resolved)
    assert summary[verify_active.STATUS_VERIFIED] == 1
    assert summary[verify_active.STATUS_INACTIVE] == 1
    assert summary[verify_active.STATUS_UNVERIFIED] == 1
    assert summary[verify_active.STATUS_FAILED] == 1
    assert summary["verified_total"] == 4


# ------------------------------------------------------------------ seen history ("new competitors only")
def test_seen_history_skips_earlier_weeks_but_never_the_current_week(tmp_path):
    seen = tmp_path / "seen_before.csv"
    seen.write_text("key,first_seen_period\nold-brand.fr,2026-10-13\nsame-week.fr,2026-10-20\n",
                    encoding="utf-8")
    history = wh_scan.load_seen_before(period="2026-10-20", path=str(seen))
    assert history == {"old-brand.fr"}                 # the current period never filters itself


def test_seen_history_does_not_drop_the_second_ad_of_a_page(monkeypatch):
    monkeypatch.setattr(wh_scan, "SEEN", {"old-brand.fr"})
    data = state()
    wh_scan.add_ads(data, [ad(ad_archive_id="x1"), ad(ad_archive_id="x2"),
                           ad(page_id="222", urlStore="https://old-brand.fr", ad_archive_id="y1")], "FR")
    assert brand_of(data)["unique_ads_count"] == 2     # both ads of a new page are counted
    assert "222" not in data["brands"]                 # a page already reported earlier is skipped


def test_seen_history_is_written_once_per_key_and_keeps_old_rows(tmp_path):
    seen = tmp_path / "seen_before.csv"
    seen.write_text("domain,first_seen_period\nold-brand.fr,2026-10-05\n", encoding="utf-8")
    data = state()
    wh_scan.add_ads(data, [ad()], "FR")
    first = wh_scan.record_seen(data, "2026-10-20", path=str(seen))
    again = wh_scan.record_seen(data, "2026-10-20", path=str(seen))
    text = seen.read_text(encoding="utf-8")
    assert first == 2 and again == 0                   # domain + page id, only once
    assert "old-brand.fr,2026-10-05" in text and "maison-test.fr,2026-10-20" in text
    assert "pid:111,2026-10-20" in text


# ------------------------------------------------------------------ fresh runs / exact verification
def test_scan_starts_from_zero_unless_resume_is_asked(monkeypatch, tmp_path):
    restored, launched = [], []
    monkeypatch.setattr(run_weekly, "restore_progress", lambda *a, **k: restored.append(a[0]))
    monkeypatch.setattr(run_weekly, "save_progress", lambda *a, **k: None)
    monkeypatch.setattr(run_weekly, "_run", lambda cmd, **k: launched.append(cmd) or 0)
    failed, partial = run_weekly.scan_countries(["FR", "US"], "2026-10-20", "2026-10-26", tmp_path, 5000, 60)
    assert restored == [] and failed == [] and partial == [] and len(launched) == 2
    run_weekly.scan_countries(["FR"], "2026-10-20", "2026-10-26", tmp_path, 5000, 60, resume=True)
    assert restored == ["FR"]


def test_country_tab_allocation_uses_five_tab_budget_without_exceeding_it(monkeypatch):
    monkeypatch.setenv("WH_COUNTRY_WORKERS", "5")
    monkeypatch.setenv("WH_VERIFY_WORKERS", "4")
    expected = {
        1: [4],
        2: [3, 2],
        3: [2, 2, 1],
        4: [2, 1, 1, 1],
        5: [1, 1, 1, 1, 1],
        6: [1, 1, 1, 1, 1, 1],
    }
    for countries, allocation in expected.items():
        actual = run_weekly._country_tab_allocation(countries)
        assert actual == allocation
        assert sum(actual[:run_weekly._country_workers(countries)]) <= 5
    monkeypatch.setenv("WH_VERIFY_WORKERS", "1")
    assert run_weekly._country_tab_allocation(1) == [1]
    assert run_weekly._country_tab_allocation(6) == [1, 1, 1, 1, 1, 1]


def test_stylesheets_are_aborted_with_other_nonessential_resources():
    assert verify_active.HEAVY_RESOURCES == {"image", "media", "font", "stylesheet"}


def test_scan_never_hides_a_store_seen_in_an_earlier_week(monkeypatch, tmp_path):
    seen_env = {}
    monkeypatch.setattr(run_weekly, "save_progress", lambda *a, **k: None)
    monkeypatch.setattr(run_weekly, "_run", lambda cmd, env=None, **k: seen_env.update(env or {}) or 0)
    monkeypatch.delenv("WH_SEEN_FILE", raising=False)
    run_weekly.scan_countries(["FR"], "2026-10-20", "2026-10-26", tmp_path, 5000, 60)
    assert "WH_SEEN_FILE" not in seen_env            # history filter is OFF by default
    assert wh_scan.load_seen_before(period="2026-10-20", path="") == set()


def test_live_verification_rechecks_every_pending_page_with_a_time_budget(monkeypatch, tmp_path):
    import types, verify_active
    captured = {}
    monkeypatch.setattr(wh_scan, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(verify_active, "verify_targets",
                        lambda targets, **kw: captured.update(kw, n=len(targets)) or {})
    monkeypatch.setenv("WH_DEADLINE", str(time.time() + 3600))
    monkeypatch.setenv("WH_COUNTRIES_LEFT", "2")
    monkeypatch.setenv("WH_VERIFY_LIMIT", "0")
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111"), ad(page_id="222", urlStore="https://b-shop.fr")], "FR")
    wh_scan.resolve_state_verification(data, "FR", types.SimpleNamespace(verify_max_age=None), live=True)
    assert captured["n"] == 2                         # no cap: every unverified page is asked
    assert captured["refresh"] is True                # an old cached number is never reused
    assert 1500 < captured["deadline"] - time.time() <= 1800   # an equal share of the time left


def test_keep_all_stores_switch_keeps_non_fashion_stores(monkeypatch, tmp_path):
    import types
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WH_KEEP_ALL_STORES", "1")
    monkeypatch.setattr(wh_scan, "CACHE_FILE", write_cache(
        tmp_path / "cache.json", {"FR:111": cache_entry(3)}))
    data = state()
    wh_scan.add_ads(data, [ad(pageName="Garden Candles Home", urlStore="https://candles-home.fr",
                              copy="scented candles for your kitchen")], "FR")
    a = types.SimpleNamespace(country="FR", verify_live=False, verify_max_age=None, top_score=3,
                              date_from="2026-10-20", date_to="2026-10-26")
    wh_scan.export(data, a, str(tmp_path / "out.xlsx"))
    import openpyxl
    wb = openpyxl.load_workbook(tmp_path / "out.xlsx")
    sites = [r[1] for ws in (wb["Top"], wb["Others"]) for r in ws.iter_rows(min_row=2, values_only=True)]
    assert any("candles-home.fr" in str(s) for s in sites)


# ------------------------------------------------------------------ the "0 active ads but Meta shows 52" bug
class _Clock:
    """Virtual time: the polling loop of check_page runs instantly in the tests."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class _FakePage:
    def __init__(self, clock, script, log):
        self.clock, self.script, self.log, self.url = clock, script, log, ""

    async def goto(self, url, **kwargs):
        self.url = url
        self.log.append(url)

    async def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000.0

    async def inner_text(self, selector):
        return self.script(self.url, self.clock.t)

    async def close(self):
        pass


class _FakeContext:
    def __init__(self, script):
        self.clock, self.script, self.urls = _Clock(), script, []

    async def new_page(self):
        return _FakePage(self.clock, self.script, self.urls)


NAV = "Meta\nAd Library\nFilters\nSort by\nWeston & Houston\n"
EMPTY_PAGE = NAV + "No ads match your search criteria"
LIVE_PAGE = NAV + "~52 results\nActive\nLibrary ID: 1747747063195807\nStarted running on 4 Oct 2026"
ALL_ADS_INACTIVE = (NAV + "~340 results\nInactive\nLibrary ID: 9\nStarted running on 1 Jan 2026\n"
                    "Ended 20 Jan 2026")


def run_check(script, **kwargs):
    context = _FakeContext(script)
    result = asyncio.run(verify_active.check_page(context, "1010436445480453", "US",
                                                  expect="Weston & Houston",
                                                  clock=context.clock.now, **kwargs))
    return result, context


def test_parse_body_counter_and_ad_cards_win_over_the_empty_sentence():
    both = NAV + "No ads match your search criteria\n~52 results"
    assert verify_active.parse_body(both) == ("live", 52)                # the template text must not win
    cards_only = NAV + "No ads match your search criteria\nLibrary ID: 5\nStarted running on 4 Oct 2026"
    assert verify_active.parse_body(cards_only)[0] == "unknown"           # ads on screen = not an empty page
    assert verify_active.parse_body(EMPTY_PAGE) == ("inactive", 0)
    copy = NAV + "Library ID: 1\nGet 3 results in a week\n"
    assert verify_active.parse_body(copy)[0] == "unknown"                 # ad copy is not the counter


def test_a_transient_empty_state_is_not_read_as_zero():
    """The reported bug: Meta shows the empty template for a moment, then the real '~52 results'."""
    result, _ = run_check(lambda url, t: EMPTY_PAGE if t < 3 else LIVE_PAGE)
    assert result["state"] == "live" and result["live"] == 52
    assert result["approximate"] is True


def test_counter_approximation_marker_matches_the_selected_counter_only():
    exact_with_unrelated_ad_copy = NAV + "52 results\nActive\nLibrary ID: 5\n~99 results in a week"
    exact, _ = run_check(lambda url, t: exact_with_unrelated_ad_copy)
    rounded, _ = run_check(lambda url, t: LIVE_PAGE)
    assert exact["state"] == "live" and exact["live"] == 52 and exact["approximate"] is False
    assert rounded["state"] == "live" and rounded["live"] == 52 and rounded["approximate"] is True


def test_a_confirmed_zero_needs_a_stable_empty_state_and_the_all_ads_view():
    def script(url, t):
        return ALL_ADS_INACTIVE if "active_status=all" in url else EMPTY_PAGE
    result, context = run_check(script)
    assert result["state"] == "inactive" and result["live"] == 0 and result["confirmed"] is True
    assert result["all_ads"] == 340
    assert any("active_status=all" in url for url in context.urls)       # the second look really happened


def test_an_empty_answer_without_proof_is_unknown_never_zero():
    # Meta empty for the active view AND for the all-ads view: a block, not an inactive advertiser
    result, _ = run_check(lambda url, t: EMPTY_PAGE)
    assert result["live"] is None and result["state"] == "unknown"
    assert result["error"] == verify_active.ZERO_UNCONFIRMED
    # the all-ads view contradicts the empty active view
    contradiction = NAV + "~12 results\nActive\nLibrary ID: 3\nStarted running on 2 Oct 2026"
    result, _ = run_check(lambda url, t: contradiction if "active_status=all" in url else EMPTY_PAGE)
    assert result["live"] is None and "contradiction" in result["error"]
    # the advertiser name is missing from the empty page: rate limited, as before
    result, _ = run_check(lambda url, t: "Meta\nFilters\nSort by\nNo ads match your search criteria")
    assert result["live"] is None and result["error"] == verify_active.RATE_LIMITED


def test_legacy_unconfirmed_zero_is_not_trusted_and_can_be_purged(tmp_path):
    legacy = cache_entry(0, "inactive", confirmed=False)               # what the old code wrote
    cache = write_cache(tmp_path / "cache.json", {"US:1": dict(legacy, page_id="1", country="US"),
                                                  "US:2": cache_entry(7)})
    entry = verify_active.resolve_pages([("1", "US")], cache_path=cache)["US:1"]
    assert entry["status"] == verify_active.STATUS_UNVERIFIED and entry["verified"] is None
    assert verify_active.cached_live(verify_active.load_cache(cache), "1", "US") is None
    assert verify_active.cached_live(verify_active.load_cache(cache), "2", "US") == 7
    assert verify_active.purge_legacy_zeros(cache) == 1
    assert sorted(verify_active.load_cache(cache)) == ["US:2"]


def test_a_confirmed_zero_expires_like_any_other_answer(tmp_path):
    old = (NOW - dt.timedelta(hours=200)).isoformat()
    cache = write_cache(tmp_path / "cache.json", {"FR:111": cache_entry(0, "inactive", checked=old)})
    entry = verify_active.resolve_pages([("111", "FR")], cache_path=cache, now=NOW)["FR:111"]
    assert entry["status"] == verify_active.STATUS_STALE and entry["verified"] is None


def test_report_link_is_the_url_that_was_verified():
    assert (wh_scan.adlib_link("9", "US") == run_weekly.adlib_url("9", "US")
            == verify_active.adlib_url("9", "US", active=True))
    assert "active_status=all" in verify_active.adlib_url("9", "US", active=False)


def test_unverified_pages_are_disclosed_in_the_header_and_in_the_excel(tmp_path):
    rows = [workbook_row(page_id="111", site="live.fr", name="Live Brand", verified=5),
            workbook_row(page_id="222", site="unverified.fr", name="Unknown Brand",
                         verified=None, status="unverified")]
    _, results, run = merge_fixture(tmp_path, rows)
    assert run["outcome"] == "complete" and run["verification"] == "partial"
    assert "dash" in run["note"]
    text, _ = html_payload(results / "competitors_week_2026-10-20.html")
    assert "PARTIAL VERIFICATION" in text
    wb = openpyxl.load_workbook(results / "competitors_week_2026-10-20.xlsx")
    body = list(wb["All Top"].iter_rows(values_only=True))
    head = {name: i for i, name in enumerate(body[0])}
    by_site = {str(r[head["Site link"]]): r for r in body[1:]}
    row = by_site["https://unverified.fr"]
    assert row[head["Verification"]] == "Unverified"
    assert row[head["Verified active ads"]] in (None, "")       # no number, and in particular no fake 0




# ------------------------------------------------------------------ rate-limit cool-down + verify mode
def test_rate_limited_pages_are_retried_after_a_cool_down(monkeypatch, tmp_path):
    """After a source block, the same unanswered pages retry on the same route after cooldown."""
    import sys
    import types

    attempts = {}
    launches, contexts = [], []

    async def fake_check(context, page_id, country, expect=None, **kwargs):
        attempts[page_id] = attempts.get(page_id, 0) + 1
        if attempts[page_id] == 1:
            return {"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED}
        return {"state": "live", "live": 5, "error": None}

    class _Ctx:
        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **kwargs):
            contexts.append(kwargs.get("proxy"))
            return _Ctx()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kwargs):
            launches.append(kwargs.get("proxy"))
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self):
            return _Playwright()

        async def __aexit__(self, *exc):
            return False

    fake_module = types.ModuleType("playwright.async_api")
    setattr(fake_module, "async_playwright", lambda: _Manager())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    monkeypatch.setattr(verify_active, "check_page", fake_check)
    monkeypatch.setenv("WH_RATE_PAUSE_SECONDS", "5")
    monkeypatch.setenv("WH_RATE_PAUSE_MAX", "5")
    monkeypatch.setenv("WH_SWEEP_PASSES", "0")
    monkeypatch.setenv("WH_PROXY", "http://u:p@gw.example:8000")
    targets = [(str(i), "FR", f"Brand {i}") for i in range(1, 4)]
    cache = verify_active.verify_targets(targets, workers=2, cache_path=tmp_path / "cache.json",
                                         refresh=True, gap=0, retries=0)
    assert all(cache[f"FR:{i}"]["live"] == 5 for i in range(1, 4))
    assert attempts == {"1": 2, "2": 2, "3": 2}
    assert launches == [{"server": "http://gw.example:8000", "username": "u", "password": "p"}]
    assert contexts == [None]                         # cooldown keeps the same browser context and route


def test_verify_mode_gives_the_live_export_a_time_budget(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(run_weekly, "restore_progress", lambda *a, **k: None)
    monkeypatch.setattr(run_weekly, "_run", lambda cmd, env=None, **k: launched.append(env) or 0)
    work = tmp_path / "work"
    work.mkdir()
    for country in ("FR", "US"):
        (work / f"scan_{country}_2026-10-20_2026-10-26.json").write_text("{}", encoding="utf-8")
    failed = run_weekly.export_countries(["FR", "US"], "2026-10-20", "2026-10-26", work, max_minutes=60)
    assert failed == [] and len(launched) == 2
    assert launched[0]["WH_COUNTRIES_LEFT"] == "2" and launched[1]["WH_COUNTRIES_LEFT"] == "1"
    assert float(launched[0]["WH_DEADLINE"]) > time.time() + 3000


# ------------------------------------------------------------------ speed: same report, fewer downloads
def _png_bytes(color):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buffer, "PNG")
    return buffer.getvalue()


def test_picture_cache_gives_identical_bytes_and_skips_the_second_download(monkeypatch, tmp_path):
    import requests
    downloads = []

    class _Resp:
        content = _png_bytes((200, 30, 30))

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda url, timeout=0: downloads.append(url) or _Resp())
    monkeypatch.setattr(run_weekly, "THUMB_CACHE", tmp_path / "thumbs")
    first = run_weekly._fetch_thumb("https://cdn.example/a.png")
    second = run_weekly._fetch_thumb("https://cdn.example/a.png")      # served from the disk cache
    assert downloads == ["https://cdn.example/a.png"]                   # downloaded once
    assert first is not None and second is not None and first == second  # same bytes, same size: same report
    assert first[1][0] <= run_weekly.IMG_W * 2 and first[1][1] <= run_weekly.ROW_H * 2


def test_failed_picture_download_is_not_cached(monkeypatch, tmp_path):
    import requests

    def boom(url, timeout=0):
        raise requests.RequestException("down")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(run_weekly, "THUMB_CACHE", tmp_path / "thumbs")
    assert run_weekly._fetch_thumb("https://cdn.example/b.png") is None
    assert not list((tmp_path / "thumbs").glob("*")) if (tmp_path / "thumbs").exists() else True


def test_report_is_identical_with_a_cold_and_a_warm_picture_cache(monkeypatch, tmp_path):
    import requests

    class _Resp:
        content = _png_bytes((10, 120, 200))

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda url, timeout=0: _Resp())
    monkeypatch.setattr(run_weekly, "THUMB_CACHE", tmp_path / "thumbs")
    row = workbook_row(thumb="https://cdn.example/p.png")
    outputs = []
    for name in ("cold", "warm"):
        _, results, _ = merge_fixture(tmp_path / name, [row])
        _, data = html_payload(results / "competitors_week_2026-10-20.html")
        outputs.append([r["i"] for r in data["top"]])
    assert outputs[0] == outputs[1] and outputs[0][0].startswith("data:image/jpeg;base64,")


# ------------------------------------------------------------------ proxy support
def test_proxy_setting_is_parsed_for_playwright(monkeypatch):
    monkeypatch.delenv("WH_PROXY", raising=False)
    assert verify_active.parse_proxy() is None
    assert verify_active.parse_proxy("") is None
    assert verify_active.parse_proxy("http://user:p%40ss@gw.proxy.io:8000") == {
        "server": "http://gw.proxy.io:8000", "username": "user", "password": "p@ss"}
    assert verify_active.parse_proxy("gw.proxy.io:8000") == {"server": "http://gw.proxy.io:8000"}
    monkeypatch.setenv("WH_PROXY", "socks5://u:p@10.0.0.1:1080")
    parsed = verify_active.parse_proxy()
    assert parsed is not None and parsed["server"] == "socks5://10.0.0.1:1080"


def test_strict_mode_lists_only_competitors_with_a_real_meta_answer(monkeypatch):
    monkeypatch.delenv("WH_STRICT", raising=False)
    assert scoring.is_report_candidate("shop.fr", "unverified") is True
    monkeypatch.setenv("WH_STRICT", "1")
    assert scoring.is_report_candidate("shop.fr", "unverified") is False
    assert scoring.is_report_candidate("shop.fr", "stale") is False
    assert scoring.is_report_candidate("shop.fr", "verified") is True
    assert scoring.is_report_candidate("shop.fr", "inactive") is True


def test_final_sweep_leaves_no_page_unverified(monkeypatch, tmp_path):
    """A page whose advertiser name never matches (looks 'rate limited') is resolved by the last sweep."""
    import sys
    import types

    seen = {"8": 0}

    async def fake_check(context, page_id, country, expect=None, **kwargs):
        if page_id == "9":                                   # name mismatch + nothing in the library
            if expect:
                return {"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED}
            return {"state": "unknown", "live": None, "error": verify_active.ZERO_UNCONFIRMED}
        if page_id == "8":                                   # throttled once, fine afterwards
            seen["8"] += 1
            if seen["8"] == 1:
                return {"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED}
        return {"state": "live", "live": 5, "error": None}

    class _Ctx:
        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **kwargs):
            return _Ctx()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self):
            return _Playwright()

        async def __aexit__(self, *exc):
            return False

    fake_module = types.ModuleType("playwright.async_api")
    setattr(fake_module, "async_playwright", lambda: _Manager())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    monkeypatch.setattr(verify_active, "check_page", fake_check)
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    targets = [("1", "FR", "Control"), ("8", "FR", "Throttled once"), ("9", "FR", "Mismatch Name")]
    cache = verify_active.verify_targets(targets, workers=1, cache_path=tmp_path / "cache.json",
                                         refresh=True, gap=0, retries=0)
    assert cache["FR:8"]["live"] == 5                                    # asked again by the sweep
    assert cache["FR:9"]["live"] == 0 and cache["FR:9"]["confirmed"] is True
    entry = verify_active.resolve_pages([("9", "FR")], cache_path=tmp_path / "cache.json")["FR:9"]
    assert entry["status"] == verify_active.STATUS_INACTIVE              # published as a verified zero


def test_final_sweep_never_invents_a_zero_while_meta_blocks_us(monkeypatch, tmp_path):
    import sys
    import types

    async def blocked(context, page_id, country, expect=None, **kwargs):
        return {"state": "unknown", "live": None, "error": verify_active.ZERO_UNCONFIRMED}

    class _C:
        async def close(self):
            pass

    class _B:
        async def new_context(self, **kwargs):
            return _C()

        async def close(self):
            pass

    class _Ch:
        async def launch(self, **kwargs):
            return _B()

    class _P:
        chromium = _Ch()

    class _M:
        async def __aenter__(self):
            return _P()

        async def __aexit__(self, *exc):
            return False

    module = types.ModuleType("playwright.async_api")
    setattr(module, "async_playwright", lambda: _M())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(verify_active, "check_page", blocked)
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    cache = verify_active.verify_targets([("9", "FR", "X")], workers=1, cache_path=tmp_path / "c.json",
                                         refresh=True, gap=0, retries=0)
    assert cache["FR:9"]["live"] is None                                 # no control page answered: stays unknown


def test_finish_verification_repeats_until_every_page_has_an_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_VERIFY_LIVE", "1")
    monkeypatch.setenv("WH_VERIFY_ROUND_WAIT", "0")
    monkeypatch.setattr(run_weekly.time, "sleep", lambda seconds: None)
    left = {"FR": 3, "US": 0}
    rounds = []

    def fake_export(countries, start, end, work, max_minutes=335):
        rounds.append(list(countries))
        left["FR"] = max(0, left["FR"] - 2)                   # every round answers 2 more pages
        return []

    monkeypatch.setattr(run_weekly, "unverified_left", lambda c, *a: left.get(c, 0))
    monkeypatch.setattr(run_weekly, "export_countries", fake_export)
    run_weekly.finish_verification(["FR", "US"], "2026-10-20", "2026-10-26", tmp_path,
                                   time.time() + 3600)
    assert rounds == [["FR"], ["FR"]]                         # US was complete: never asked again
    assert left["FR"] == 0


def test_finish_verification_stops_when_the_time_budget_is_used_up(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_VERIFY_LIVE", "1")
    monkeypatch.setattr(run_weekly, "unverified_left", lambda c, *a: 5)
    called = []
    monkeypatch.setattr(run_weekly, "export_countries", lambda *a, **k: called.append(1) or [])
    run_weekly.finish_verification(["FR"], "2026-10-20", "2026-10-26", tmp_path, time.time() + 60)
    assert called == []                                       # less than 8 minutes left: no new round


def test_finish_verification_is_off_without_live_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_VERIFY_LIVE", "0")
    monkeypatch.setattr(run_weekly, "unverified_left", lambda c, *a: 5)
    called = []
    monkeypatch.setattr(run_weekly, "export_countries", lambda *a, **k: called.append(1) or [])
    run_weekly.finish_verification(["FR"], "2026-10-20", "2026-10-26", tmp_path, time.time() + 3600)
    assert called == []


def test_unverified_left_reads_the_country_workbook(tmp_path):
    write_country_workbook(tmp_path / "competitors_FR_2026-10-20_2026-10-26.xlsx",
                           [workbook_row(site="unverified.fr", verified=None, status="unverified"),
                            workbook_row(page_id="2", site="live.fr", verified=5)])
    assert run_weekly.unverified_left("FR", "2026-10-20", "2026-10-26", tmp_path) >= 0
    assert run_weekly.unverified_left("US", "2026-10-20", "2026-10-26", tmp_path) == 0   # no workbook


def test_all_pages_share_one_non_rotating_proxy_route(monkeypatch, tmp_path):
    import sys
    import types

    launches, contexts = [], []

    async def fake_check(context, page_id, country, expect=None, **kwargs):
        return {"state": "live", "live": 3, "error": None}

    class _Ctx:
        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **kwargs):
            contexts.append(kwargs.get("proxy"))
            return _Ctx()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kwargs):
            launches.append(kwargs.get("proxy"))
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self):
            return _Playwright()

        async def __aexit__(self, *exc):
            return False

    module = types.ModuleType("playwright.async_api")
    setattr(module, "async_playwright", lambda: _Manager())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(verify_active, "check_page", fake_check)
    monkeypatch.setenv("WH_PROXY", "http://user:pw@gw.example:8000")
    targets = [(str(i), "FR", f"B{i}") for i in range(1, 6)]
    cache = verify_active.verify_targets(targets, workers=2, cache_path=tmp_path / "c.json",
                                         refresh=True, gap=0, retries=0)
    assert all(cache[f"FR:{i}"]["live"] == 3 for i in range(1, 6))
    assert launches == [{"server": "http://gw.example:8000", "username": "user", "password": "pw"}]
    assert len(contexts) == 1 and contexts[0] is None                   # one shared context, no rotation


# ------------------------------------------------------------------ F: only pages that can be listed go to Meta
def test_only_listed_pages_are_checked_on_meta_and_top_stores_come_first(tmp_path, monkeypatch):
    monkeypatch.setattr(wh_scan, "CACHE_FILE", write_cache(tmp_path / "cache.json", {}))
    monkeypatch.setenv("WH_VERIFY_SCOPE", "reportable")
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111", urlStore="https://small-brand.fr"),
                           ad(page_id="222", urlStore="https://big-brand.fr"),
                           ad(page_id="333", urlStore="https://www.facebook.com/somepage"),   # platform link
                           ad(page_id="444", urlStore="")], "FR")                              # no site at all
    data["brands"]["111"]["api_reported_count"] = 3
    data["brands"]["222"]["api_reported_count"] = 90
    asked = []
    monkeypatch.setattr(verify_active, "verify_targets",
                        lambda targets, **kwargs: asked.append([t[0] for t in targets]) or {})
    args = argparse.Namespace(country="FR", date_from="2026-10-20", date_to="2026-10-26",
                              top_score=5, verify_live=True, verify_max_age=None)
    wh_scan.resolve_state_verification(data, "FR", args, live=True)
    assert len(asked) == 1
    assert sorted(asked[0]) == ["111", "222"]            # 333 (platform link) and 444 (no site) are not asked
    assert wh_scan.listed_page_ids(data) == {"111", "222"}


def test_scope_all_restores_the_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("WH_VERIFY_SCOPE", "all")
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111", urlStore="https://small-brand.fr"),
                           ad(page_id="444", urlStore="")], "FR")
    assert wh_scan.listed_page_ids(data) == {"111", "444"}


def test_pages_without_a_store_site_do_not_count_as_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(wh_scan, "CACHE_FILE", write_cache(tmp_path / "cache.json",
                                                           {"FR:111": cache_entry(4)}))
    monkeypatch.setenv("WH_VERIFY_SCOPE", "reportable")
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111", urlStore="https://live-brand.fr"),
                           ad(page_id="444", urlStore="")], "FR")
    args = argparse.Namespace(country="FR", date_from="2026-10-20", date_to="2026-10-26",
                              top_score=5, verify_live=False, verify_max_age=None)
    path = tmp_path / "country.xlsx"
    wh_scan.export(data, args, str(path))
    wb = openpyxl.load_workbook(path, read_only=True)
    meta = {r[0]: r[1] for r in wb["Meta"].iter_rows(values_only=True) if r[0]}
    assert meta["unverified"] == 0 and meta["verified"] == 1
    assert meta["pages_without_store_site"] == 1


def _fake_playwright(monkeypatch, check, goto_error=None):
    import sys
    import types

    class _Page:
        async def goto(self, url, **kwargs):
            if goto_error and "ipify" in url:
                raise RuntimeError(goto_error)

        async def inner_text(self, selector):
            return '{"ip":"203.0.113.9"}'

    class _Ctx:
        async def new_page(self):
            return _Page()

        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **kwargs):
            return _Ctx()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self):
            return _Playwright()

        async def __aexit__(self, *exc):
            return False

    module = types.ModuleType("playwright.async_api")
    setattr(module, "async_playwright", lambda: _Manager())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(verify_active, "check_page", check)


def test_finish_verification_retries_no_progress_until_the_pages_are_complete(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_VERIFY_LIVE", "1")
    monkeypatch.setenv("WH_VERIFY_ROUND_WAIT", "0")
    monkeypatch.setenv("WH_VERIFY_ROUNDS", "0")
    monkeypatch.setattr(run_weekly.time, "sleep", lambda seconds: None)
    checks = []
    def still_unanswered_then_complete(country, *args):
        checks.append(country)
        return 7 if len(checks) <= 4 else 0
    monkeypatch.setattr(run_weekly, "unverified_left", still_unanswered_then_complete)
    rounds = []
    monkeypatch.setattr(run_weekly, "export_countries", lambda *a, **k: rounds.append(1) or [])
    run_weekly.finish_verification(["FR"], "2026-10-20", "2026-10-26", tmp_path, 0)
    assert len(rounds) == 2 and len(checks) == 5              # no stall ceiling; stops when the source answers


# ------------------------------------------------------------------ automatic rounds (same runner and route)
def _manifest(run_id="42", unverified=10, stale=0, failed=0):
    return {"workflow_run_id": run_id, "unverified_pages": unverified, "stale_pages": stale,
            "verification_failed": failed, "from": "2026-10-19", "to": "2026-10-25",
            "countries": ["US", "FR"]}


def test_chain_continues_while_pages_are_unverified_and_stops_when_done():
    import chain_control
    state = chain_control.fresh_state(1000)
    go, reason, state = chain_control.decide(state, _manifest(unverified=500), "42", 2000)
    assert go and state["depth"] == 1 and state["last"] == 500
    go, reason, state = chain_control.decide(state, _manifest(unverified=300), "42", 3000)
    assert go and state["depth"] == 2
    go, reason, state = chain_control.decide(state, _manifest(unverified=0), "42", 4000)
    assert not go and "done" in reason and state["depth"] == 0          # finished: the chain is reset


def test_chain_has_no_implicit_stall_time_or_round_limit():
    import chain_control
    # no report of this run -> never continue (a failed run must not loop)
    go, reason, _ = chain_control.decide(chain_control.fresh_state(0), _manifest(run_id="old"), "42", 10)
    assert not go and "no report" in reason
    # Repeated no-progress results continue by default; source throttles are not a round limit.
    state = chain_control.fresh_state(0)
    go, _, state = chain_control.decide(state, _manifest(unverified=100), "42", 10)
    assert go
    for n in range(20):
        go, reason, state = chain_control.decide(state, _manifest(unverified=100), "42", 20 + n)
        assert go, reason
    # Explicit limits remain available to callers that opt in.
    go, reason, _ = chain_control.decide(chain_control.fresh_state(0), _manifest(), "42", 9 * 3600,
                                         max_hours=8)
    assert not go and "time limit" in reason
    state = dict(chain_control.fresh_state(0), depth=15)
    go, reason, _ = chain_control.decide(state, _manifest(), "42", 100, max_rounds=15)
    assert not go and "rounds" in reason
    state = chain_control.fresh_state(0)
    go, _, state = chain_control.decide(state, _manifest(unverified=100), "42", 10, max_stalls=2)
    go, _, state = chain_control.decide(state, _manifest(unverified=100), "42", 20, max_stalls=2)
    go, reason, _ = chain_control.decide(state, _manifest(unverified=100), "42", 30, max_stalls=2)
    assert not go and "no progress" in reason


def test_chain_begin_continues_a_running_chain_or_starts_a_new_one():
    import chain_control
    running = {"started": 1000, "ended": 5000, "depth": 3, "last": 80, "stalls": 0}
    assert chain_control.begin_state(running, 5000 + 10 * 60)["depth"] == 3          # next run, minutes later
    assert chain_control.begin_state(running, 5000 + 5 * 3600)["depth"] == 0         # a client started a new run
    assert chain_control.begin_state({}, 5000)["depth"] == 0


def test_chain_writes_the_github_outputs(monkeypatch, tmp_path):
    import chain_control
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(chain_control, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(chain_control, "MANIFEST_FILE", tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest(unverified=50)), encoding="utf-8")
    assert chain_control.main(["end"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "continue=true" in text and "date_from=2026-10-19" in text and "countries=US,FR" in text


def test_a_stale_block_marker_does_not_suppress_a_new_verification(monkeypatch, tmp_path):
    marker = tmp_path / "blocked"
    marker.write_text("old source block", encoding="utf-8")
    monkeypatch.setenv("WH_BLOCK_MARKER", str(marker))
    monkeypatch.setenv("WH_SWEEP_PASSES", "0")
    calls = []

    async def recovered(context, page_id, country, expect=None, **kwargs):
        calls.append(page_id)
        return {"state": "live", "live": 6, "error": None}

    _fake_playwright(monkeypatch, recovered)
    cache = verify_active.verify_targets([("99", "US", "Other")], workers=1,
                                         cache_path=tmp_path / "c.json", refresh=True, gap=0, retries=0)
    assert calls == ["99"] and cache["US:99"]["live"] == 6


def test_a_proxy_run_ignores_the_blocked_marker(monkeypatch, tmp_path):
    marker = tmp_path / "blocked"
    marker.write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setenv("WH_BLOCK_MARKER", str(marker))
    monkeypatch.setenv("WH_PROXY", "http://u:p@gw.example:8000")

    async def check(context, page_id, country, expect=None, **kwargs):
        return {"state": "live", "live": 4, "error": None}

    _fake_playwright(monkeypatch, check)
    cache = verify_active.verify_targets([("1", "FR", "A")], workers=1, cache_path=tmp_path / "c.json",
                                         refresh=True, gap=0, retries=0)
    assert cache["FR:1"]["live"] == 4


def test_zero_round_limit_continues_until_all_pages_are_answered(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_VERIFY_LIVE", "1")
    monkeypatch.setenv("WH_VERIFY_ROUNDS", "0")
    monkeypatch.setenv("WH_VERIFY_ROUND_WAIT", "0")
    remaining = {"pages": 9}
    called = []

    def unverified_left(country, *args):
        return remaining["pages"]

    def export_countries(*args, **kwargs):
        called.append(1)
        remaining["pages"] = 0
        return []

    monkeypatch.setattr(run_weekly, "unverified_left", unverified_left)
    monkeypatch.setattr(run_weekly, "export_countries", export_countries)
    run_weekly.finish_verification(["FR"], "2026-10-20", "2026-10-26", tmp_path, 0)
    assert called == [1]


def test_blocks_that_recover_never_exhaust_the_cool_down_budget(monkeypatch, tmp_path):
    """Three separate blocks, each followed by normal answers: the old 'total cool-downs' rule would give up at 2,
    the new rule only counts cool-downs IN A ROW (Meta answering again resets it), so every page gets answered."""
    import sys
    import types

    calls = []
    pattern = {}                                  # call number -> throttled?
    for start in (1, 21, 41):                     # 8 throttled answers, then 12+ good ones, three times
        for n in range(start, start + 8):
            pattern[n] = True

    async def fake_check(context, page_id, country, expect=None, **kwargs):
        calls.append(page_id)
        if pattern.get(len(calls)):
            return {"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED}
        return {"state": "live", "live": 7, "error": None}

    class _Ctx:
        async def close(self): pass

    class _Browser:
        async def new_context(self, **kw): return _Ctx()
        async def close(self): pass

    class _Chromium:
        async def launch(self, **kw): return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self): return _Playwright()
        async def __aexit__(self, *exc): return False

    fake_module = types.ModuleType("playwright.async_api")
    setattr(fake_module, "async_playwright", lambda: _Manager())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    monkeypatch.setattr(verify_active, "check_page", fake_check)
    monkeypatch.setenv("WH_RATE_PAUSE_SECONDS", "0.01")
    monkeypatch.setenv("WH_RATE_PAUSES", "2")
    targets = [(str(i), "FR", f"Brand {i}") for i in range(1, 41)]
    cache = verify_active.verify_targets(targets, workers=1, cache_path=tmp_path / "cache.json",
                                         refresh=True, gap=0, retries=0)
    assert all(cache[f"FR:{i}"]["live"] == 7 for i in range(1, 41))

# ------------------------------------------------------------------ official Meta API (exact counts, EU/UK)
class _ApiResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self._payload


class _ApiSession:
    """Plays back scripted answers and remembers every request."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params=None, timeout=0):
        self.calls.append((url, dict(params or {})))
        return self.answers.pop(0)


def _ads(page_id, n, start=0):
    return [{"id": f"{page_id}{start + i}", "page_id": page_id} for i in range(n)]


def test_meta_api_counts_every_active_ad_and_gives_an_authoritative_zero(monkeypatch):
    import meta_api
    monkeypatch.setattr(meta_api, "_sleep", lambda seconds: None)
    # page 111: 3 ads over two result pages (one id repeated), page 222: none at all, page 333: 1 ad
    first = {"data": _ads("111", 2) + _ads("333", 1),
             "paging": {"next": "https://graph.facebook.com/next-page"}}
    second = {"data": _ads("111", 2, start=1)}                    # id 1111 is repeated: counts once
    session = _ApiSession([_ApiResp(first), _ApiResp(second)])
    counts = meta_api.count_active("fr", ["111", "222", "333"], access_token="T", session=session)
    assert counts == {"111": 3, "222": 0, "333": 1}
    url, params = session.calls[0]
    assert params["ad_active_status"] == "ACTIVE" and params["ad_reached_countries"] == '["FR"]'
    assert params["search_page_ids"] == "111,222,333" and session.calls[1][0].endswith("next-page")


def test_meta_api_batches_ten_pages_and_skips_countries_it_does_not_cover(monkeypatch):
    import meta_api
    monkeypatch.setattr(meta_api, "_sleep", lambda seconds: None)
    session = _ApiSession([_ApiResp({"data": []}), _ApiResp({"data": []})])
    ids = [str(i) for i in range(1, 16)]
    counts = meta_api.count_active("IT", ids, access_token="T", session=session)
    assert len(session.calls) == 2 and len(counts) == 15 and set(counts.values()) == {0}
    assert session.calls[0][1]["search_page_ids"].count(",") == 9
    assert meta_api.count_active("US", ["1"], access_token="T", session=session) == {}   # US is not in the API
    assert meta_api.supports("gb") and meta_api.supports("NL") and not meta_api.supports("US")


def test_meta_api_never_turns_an_error_into_a_zero(monkeypatch):
    import meta_api
    monkeypatch.setattr(meta_api, "_sleep", lambda seconds: None)
    monkeypatch.setenv("META_API_BACKOFF", "0")
    limited = _ApiResp({"error": {"code": 613, "message": "Calls to this api have exceeded the rate limit."}})
    ok = _ApiResp({"data": _ads("111", 2)})
    counts = meta_api.count_active("FR", ["111"], access_token="T", session=_ApiSession([limited, limited, ok]))
    assert counts == {"111": 2}                                    # rate limit: waited, asked again
    broken = [_ApiResp({}, status=500)] * 6
    assert meta_api.count_active("FR", ["111"], access_token="T", session=_ApiSession(broken)) == {}
    bad = _ApiResp({"error": {"code": 190, "message": "token expired"}})
    with pytest.raises(meta_api.ApiAuthError):
        meta_api.count_active("FR", ["111"], access_token="T", session=_ApiSession([bad]))
    ok_check, message = meta_api.check_token("T", session=_ApiSession([bad]))
    assert ok_check is False and "token" in message
    assert meta_api.check_token("T", session=_ApiSession([_ApiResp({"data": []})]))[0] is True


def test_eu_pages_are_answered_by_the_api_and_only_the_us_goes_to_the_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    import meta_api
    monkeypatch.setenv("META_API_TOKEN", "T")
    monkeypatch.delenv("WH_PROXY", raising=False)
    monkeypatch.setenv("WH_BLOCK_MARKER", str(tmp_path / "blocked"))
    asked = []
    monkeypatch.setattr(meta_api, "count_active",
                        lambda country, ids, **k: asked.append((country, list(ids))) or
                        {pid: (4 if pid == "1" else 0) for pid in ids})
    browser = []

    async def check(context, page_id, country, expect=None, **kwargs):
        browser.append((page_id, country))
        return {"state": "live", "live": 7, "error": None}

    _fake_playwright(monkeypatch, check)
    cache_path = tmp_path / "c.json"
    targets = [("1", "FR", "A"), ("2", "FR", "B"), ("3", "GB", "C"), ("9", "US", "D")]
    cache = verify_active.verify_targets(targets, workers=1, cache_path=cache_path, refresh=True, gap=0,
                                         retries=0)
    # Meta API is not on the verification path (no Facebook identity). Every page is read in the browser.
    assert asked == []
    assert sorted(browser) == [("1", "FR"), ("2", "FR"), ("3", "GB"), ("9", "US")]
    assert cache["FR:1"]["live"] == 7 and cache["FR:1"].get("source") != "meta_api"
    got = verify_active.resolve_pages([(p, c) for p, c, _ in targets], cache_path=cache_path)
    assert got["FR:1"]["status"] == verify_active.STATUS_VERIFIED and got["FR:1"]["verified"] == 7
    assert got["FR:2"]["status"] == verify_active.STATUS_VERIFIED and got["FR:2"]["verified"] == 7
    assert got["US:9"]["verified"] == 7


def test_a_refused_api_token_falls_back_to_the_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    import meta_api
    monkeypatch.setenv("META_API_TOKEN", "BAD")
    monkeypatch.delenv("WH_PROXY", raising=False)
    monkeypatch.setenv("WH_BLOCK_MARKER", str(tmp_path / "blocked"))

    def refuse(country, ids, **kwargs):
        raise meta_api.ApiAuthError("token expired")

    monkeypatch.setattr(meta_api, "count_active", refuse)

    async def check(context, page_id, country, expect=None, **kwargs):
        return {"state": "live", "live": 5, "error": None}

    _fake_playwright(monkeypatch, check)
    cache = verify_active.verify_targets([("1", "FR", "A")], workers=1, cache_path=tmp_path / "c.json",
                                         refresh=True, gap=0, retries=0)
    assert cache["FR:1"]["live"] == 5 and cache["FR:1"].get("source") != "meta_api"


# ------------------------------------------------------------------ pages that can never match by name
def test_a_page_whose_meta_name_differs_is_resolved_in_the_same_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    monkeypatch.delenv("WH_PROXY", raising=False)
    monkeypatch.delenv("META_API_TOKEN", raising=False)
    monkeypatch.setenv("WH_BLOCK_MARKER", str(tmp_path / "blocked"))

    async def check(context, page_id, country, expect=None, **kwargs):
        if page_id == "9":                                           # the name never matches, nothing runs
            return ({"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED} if expect
                    else {"state": "unknown", "live": None, "error": verify_active.ZERO_UNCONFIRMED})
        if page_id == "8" and expect:                                # name mismatch, but ads are running
            return {"state": "unknown", "live": None, "error": verify_active.RATE_LIMITED}
        return {"state": "live", "live": 6, "error": None}

    _fake_playwright(monkeypatch, check)
    targets = [("1", "FR", "Control"), ("9", "FR", "No Match"), ("8", "FR", "Other Name")]
    cache = verify_active.verify_targets(targets, workers=1, cache_path=tmp_path / "c.json", refresh=True,
                                         gap=0, retries=0)
    assert cache["FR:8"]["live"] == 6                                # counted without the name check
    assert cache["FR:9"]["live"] == 0 and cache["FR:9"]["confirmed"] is True   # control page answered: real zero


def test_without_a_working_control_page_no_zero_is_invented(monkeypatch, tmp_path):
    monkeypatch.setenv("WH_SWEEP_PAUSE_SECONDS", "0.01")
    monkeypatch.delenv("WH_PROXY", raising=False)
    monkeypatch.delenv("META_API_TOKEN", raising=False)
    monkeypatch.setenv("WH_BLOCK_MARKER", str(tmp_path / "blocked"))

    async def check(context, page_id, country, expect=None, **kwargs):
        return {"state": "unknown", "live": None, "error": verify_active.ZERO_UNCONFIRMED}

    _fake_playwright(monkeypatch, check)
    cache = verify_active.verify_targets([("9", "FR", "X")], workers=1, cache_path=tmp_path / "c.json",
                                         refresh=True, gap=0, retries=0)
    assert cache["FR:9"]["live"] is None


# ------------------------------------------------------------------ a country is never skipped / starved
def test_every_country_is_exported_even_when_the_time_budget_is_used_up(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(run_weekly, "restore_progress", lambda *a, **k: None)
    def fake_run(cmd, env=None, **kwargs):
        assert env is not None
        launched.append(env["WH_COUNTRIES_LEFT"])
        return 0
    monkeypatch.setattr(run_weekly, "_run", fake_run)
    work = tmp_path / "work"
    work.mkdir()
    for country in ("US", "FR", "NL"):
        (work / f"scan_{country}_2026-10-19_2026-10-25.json").write_text("{}", encoding="utf-8")
    failed = run_weekly.export_countries(["US", "FR", "NL"], "2026-10-19", "2026-10-25", work, max_minutes=0)
    assert failed == [] and launched == ["3", "2", "1"]              # the old code skipped the last countries


def test_the_report_row_carries_the_time_meta_was_asked(tmp_path):
    rows = [workbook_row(page_id="111", site="live.fr", name="Live Brand", verified=5,
                         checked="2026-10-30T11:22:33")]
    _, results, _ = merge_fixture(tmp_path, rows)
    _, data = html_payload(results / "competitors_week_2026-10-20.html")
    assert data["top"][0]["ck"] == "2026-10-30 11:22"                # shown when hovering the number


def test_api_pages_are_still_answered_when_the_browser_time_is_used_up(tmp_path, monkeypatch):
    import meta_api
    monkeypatch.setattr(wh_scan, "CACHE_FILE", write_cache(tmp_path / "cache.json", {}))
    monkeypatch.delenv("WH_VERIFY_SCOPE", raising=False)
    monkeypatch.setenv("META_API_TOKEN", "T")
    monkeypatch.setenv("WH_DEADLINE", str(time.time() - 5))            # the round's time is already over
    monkeypatch.setenv("WH_COUNTRIES_LEFT", "1")
    data = state()
    wh_scan.add_ads(data, [ad(page_id="111", urlStore="https://eu-brand.fr")], "FR")
    asked = []
    monkeypatch.setattr(verify_active, "verify_targets",
                        lambda targets, **kwargs: asked.append([t[0] for t in targets]) or {})
    args = argparse.Namespace(country="FR", date_from="2026-10-20", date_to="2026-10-26",
                              top_score=5, verify_live=True, verify_max_age=None)
    wh_scan.resolve_state_verification(data, "FR", args, live=True)
    assert asked == []                                 # no API: browser time is over, so FR waits too
    asked.clear()
    data = state()
    wh_scan.add_ads(data, [ad(page_id="222", urlStore="https://us-brand.com")], "US")
    args.country = "US"
    wh_scan.resolve_state_verification(data, "US", args, live=True)
    assert asked == []                                 # the US needs the browser: waits for the next round


# ------------------------------------------------------------------ all-page standalone target selection
def test_week_verification_defaults_to_both_merged_all_tabs(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    merged = result_dir / "competitors_week_2026-10-20.xlsx"
    wb = openpyxl.Workbook()
    top = wb.active
    assert top is not None
    top.title = "All Top"
    headers = ["Ads Library link", "Page name", "API reported ads"]
    top.append(headers)
    top.append([wh_scan.adlib_link("111", "FR"), "Top Brand", 100])
    others = wb.create_sheet("All Others")
    others.append(headers)
    others.append([wh_scan.adlib_link("222", "DE"), "Other Brand", 35])
    wb.save(merged)
    monkeypatch.setattr(verify_active, "RESULTS", result_dir)

    targets = verify_active.targets_from_week("2026-10-20")
    assert {(page_id, country) for page_id, country, _name in targets} == {("111", "FR"), ("222", "DE")}
    assert [page_id for page_id, _country, _name in targets] == ["111", "222"]
    assert verify_active.targets_from_week("2026-10-20", tab="All Top") == [("111", "FR", "Top Brand")]


def test_week_verification_defaults_to_both_country_workbook_tabs(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    work_dir = tmp_path / "work"
    result_dir.mkdir()
    work_dir.mkdir()
    wb = openpyxl.Workbook()
    top = wb.active
    assert top is not None
    top.title = "Top"
    headers = ["Ads Library link", "Page name", "API reported ads"]
    top.append(headers)
    top.append([wh_scan.adlib_link("333", "US"), "Top Brand", 75])
    others = wb.create_sheet("Others")
    others.append(headers)
    others.append([wh_scan.adlib_link("444", "US"), "Other Brand", 12])
    wb.save(work_dir / "competitors_US_2026-10-20_2026-10-26.xlsx")
    monkeypatch.setattr(verify_active, "RESULTS", result_dir)
    monkeypatch.setattr(verify_active, "HERE", tmp_path)

    targets = verify_active.targets_from_week("2026-10-20")
    assert {(page_id, country) for page_id, country, _name in targets} == {("333", "US"), ("444", "US")}
