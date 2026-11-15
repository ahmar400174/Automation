"""End-to-end test of the web app with a FAKE run_weekly.py (no Meta, no API, no credits)."""
# pyright: reportMissingImports=false
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="whweb_"))
os.environ["WH_ROOT"] = str(TMP)
os.environ["WH_RUN_SCRIPT"] = str(TMP / "run_weekly.py")
os.environ["WH_SKIP_PREP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAKE = r'''
import argparse, json, os, sys, time
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--countries"); ap.add_argument("--max-calls", type=int); ap.add_argument("--from", dest="f")
ap.add_argument("--to", dest="t"); ap.add_argument("--max-minutes", type=int)
ap.add_argument("--estimate", action="store_true"); ap.add_argument("--re-export", action="store_true"); ap.add_argument("--resume", action="store_true")
a = ap.parse_args()
here = Path(__file__).parent
print("fake run: countries", a.countries, "from", a.f, "to", a.t, flush=True)
if a.max_calls == 777:
    time.sleep(60)
if a.estimate:
    (here / "run_artifact").mkdir(exist_ok=True)
    (here / "run_artifact" / "estimate_report_2026-10-19.html").write_text("<html>estimate</html>")
    sys.exit(0)
counter = here / "results" / "fake_counter.txt"
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
unverified = {1: 100, 2: 60}.get(n, 0)
(here / "work").mkdir(exist_ok=True)
(here / "work" / "scan_US_x.json").write_text("{}")
html = here / "results" / "competitors_week_2026-10-19.html"; xlsx = html.with_suffix(".xlsx")
html.write_text("<html>report round %d</html>" % n); xlsx.write_bytes(b"PK fake")
(here / "results" / "run_manifest.json").write_text(json.dumps({
    "workflow_run_id": os.environ["GITHUB_RUN_ID"], "from": "2026-10-19", "to": "2026-10-25",
    "countries": ["US", "FR"], "outcome": "complete", "html_path": "results/" + html.name, "workbook_path": str(xlsx),
    "unverified_pages": unverified, "stale_pages": 0, "verification_failed": 0}))
print("tor ports:", os.environ.get("WH_TOR_PORTS"), "proxy:", bool(os.environ.get("WH_PROXY")), flush=True)
'''
(TMP / "run_weekly.py").write_text(FAKE)
(TMP / "results").mkdir(exist_ok=True)

import app as webapp_module  # noqa: E402
from webapp import runner, settings  # noqa: E402


def test_settings_migrate_old_tor_default_to_direct_once(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"use_tor": True, "run_tests": False}), encoding="utf-8")
    monkeypatch.setattr(settings, "FILE", path)
    loaded = settings.load()
    assert loaded["use_tor"] is False
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["settings_version"] == 2 and migrated["use_tor"] is False
    settings.save({"use_tor": True})
    assert settings.load()["use_tor"] is True


@pytest.fixture()
def client():
    settings.save({"use_tor": False, "chain_pause_seconds": "0", "web_password": "", "WH_API_KEY": "key123"})
    return webapp_module.app.test_client()


def wait_idle(client, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        state = client.get("/api/state").get_json()
        if not state["running"]:
            return state
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_countries_normalizer_repairs_letter_split_input():
    assert runner.normalize_countries("U,S,,, ,F,R,,, ,I,T") == ["US", "FR", "IT"]
    assert runner.normalize_countries("us, fr;gb") == ["US", "FR", "GB"]


def test_validation_errors(client):
    r = client.post("/api/start", data={"mode": "nope"})
    assert r.status_code == 400
    r = client.post("/api/start", data={"mode": "verify", "date_from": "19-10-2026"})
    assert r.status_code == 400


def test_automatic_rounds_until_everything_is_verified(client):
    r = client.post("/api/start", data={"mode": "verify", "countries": "U,S,,,F,R", "date_from": "2026-10-19",
                                        "date_to": "2026-10-25", "auto_chain": "1"})
    assert r.get_json()["ok"], r.get_json()
    state = wait_idle(client)
    job = state["history"][0]
    assert job["status"] == "finished", job
    assert any(event["title"] == "Job running" for event in job["events"])
    assert any(event["title"] == "Job finished" for event in job["events"])
    assert [x["n"] for x in job["rounds"]] == [1, 2, 3]
    assert [x["mode"] for x in job["rounds"]] == ["verify"] * 3
    assert job["rounds"][-1]["summary"]["unverified_pages"] == 0
    log = client.get(f"/api/log/{job['id']}").get_json()["text"]
    assert "ROUND 3" in log and "every page is verified" in log and "countries=US,FR" in log
    first_file = job["rounds"][0]["files"][0]
    assert client.get(f"/files/{job['id']}/round_1/{first_file}").status_code == 200
    assert (TMP / "results" / "scan_data" / "scan_US_x.json.gz").exists()
    page = client.get(f"/runs/{job['id']}")
    assert page.status_code == 200 and b"/api/job/" in page.data
    api = client.get(f"/api/job/{job['id']}").get_json()          # the job page fills its table from this
    assert api["status"] == "finished" and len(api["rounds"]) == 3 and api["rounds"][0]["files"]
    assert b"competitors_week_2026-10-19.html" in client.get("/reports").data
    archive = client.get("/api/reports").get_json()["items"]
    assert any(item["run_label"].startswith("Live report") for item in archive)
    assert any(job["id"] in item["run_label"] and item["kind"] == "html" for item in archive)


def test_single_round_without_chain_and_estimate(client):
    client.post("/api/start", data={"mode": "estimate", "countries": "US"})
    job = wait_idle(client)["history"][0]
    assert job["status"] == "finished" and job["rounds"][0]["files"] == ["estimate_report_2026-10-19.html"]


def test_live_run_snapshots_are_listed_and_opened(client):
    live = TMP / "results" / "live_runs" / "round-progress"
    live.mkdir(parents=True, exist_ok=True)
    (live / "competitors_week_2026-10-19.html").write_text("<html>in-progress</html>", encoding="utf-8")
    (live / "competitors_week_2026-10-19.xlsx").write_bytes(b"PK fake")
    items = client.get("/api/reports").get_json()["items"]
    report = next(item for item in items if item["run_label"] == "Live run snapshot · round-progress"
                  and item["kind"] == "html")
    preview = client.get(report["open_url"])
    assert preview.status_code == 200 and b"in-progress" in preview.data
    xlsx = next(item for item in items if item["run_label"] == "Live run snapshot · round-progress"
                and item["kind"] == "xlsx")
    assert client.get(xlsx["download_url"]).status_code == 200


def test_stop_button_kills_the_run(client):
    response = client.post("/api/start", data={"mode": "scan", "countries": "US", "max_calls": "777"})
    started = response.get_json()
    assert started and started["ok"]
    time.sleep(1.5)
    assert client.post("/api/stop").get_json()["ok"] is True
    assert wait_idle(client, 30)["running"] is False
    job = client.get(f"/api/job/{started['job_id']}").get_json()
    assert job is not None
    assert job["status"] == "stopped"


def test_only_one_job_and_path_traversal_blocked(client):
    client.post("/api/start", data={"mode": "scan", "countries": "US", "max_calls": "777"})
    time.sleep(1)
    r = client.post("/api/start", data={"mode": "verify"})
    assert r.status_code == 409
    client.post("/api/stop"); wait_idle(client, 30)
    assert client.get("/reports/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/files/x/../../settings.json").status_code in (404, 308)


def test_concurrent_start_is_rejected_instead_of_silently_queued(client):
    first = client.post("/api/start", data={"mode": "scan", "countries": "US", "max_calls": "777"}).get_json()
    time.sleep(0.7)
    second = client.post("/api/start", data={"mode": "verify", "countries": "FR"})
    assert second.status_code == 409 and "not queued" in second.get_json()["error"]
    client.post("/api/stop"); wait_idle(client, 30)
    assert first["job_id"]


def test_scan_needs_api_key_and_settings_hide_the_secret(client):
    settings.save({"WH_API_KEY": ""})
    os.environ.pop("WH_API_KEY", None)
    r = client.post("/api/start", data={"mode": "scan"})
    assert r.status_code == 409 and "WH_API_KEY" in r.get_json()["error"]
    settings.save({"WH_API_KEY": "supersecretvalue"})
    html = client.get("/settings").get_data(as_text=True)
    assert "supersecretvalue" not in html and "sup" in html


def test_password_protection(client):
    settings.save({"web_password": "pw"})
    assert client.get("/").status_code == 401
    import base64
    ok = client.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"user:pw").decode()})
    assert ok.status_code == 200
    settings.save({"web_password": ""})


def test_pause_minutes_validation_and_progress_endpoint(client):
    assert client.post("/api/start", data={"mode": "verify", "pause_minutes": "abc"}).status_code == 400
    params, err = runner.validate({"mode": "verify", "pause_minutes": "2", "countries": "US"})
    assert not err and params is not None and params["pause_minutes"] == 2
    params, err = runner.validate({"mode": "verify", "countries": "US"})
    assert params is not None and params["pause_minutes"] is None
    (TMP / "results" / "run_manifest.json").write_text(json.dumps(
        {"from": "2026-10-19", "to": "2026-10-25", "unverified_pages": 5, "stale_pages": 2, "outcome": "complete"}))
    data = client.get("/api/progress").get_json()
    assert data["known"] and data["unverified"] == 5 and data["stale"] == 2 and data["date_from"] == "2026-10-19"
    assert b"Continue verifying" in client.get("/").data


def test_chain_tolerates_blocked_rounds_up_to_the_limit():
    import chain_control
    state = chain_control.fresh_state(0)
    manifest = {"workflow_run_id": "r", "unverified_pages": 100, "stale_pages": 0, "verification_failed": 0}
    go, _, state = chain_control.decide(state, manifest, "r", 10, max_stalls=4)          # first round
    results, reason = [], ""
    for n in range(4):                                                                   # four blocked rounds
        go, reason, state = chain_control.decide(state, manifest, "r", 20 + n, max_stalls=4)
        results.append(go)
    assert results == [True, True, True, False] and "no progress in 4 rounds" in reason
    state = chain_control.fresh_state(0)
    go, _, state = chain_control.decide(state, manifest, "r", 10)
    assert go
    reason = ""
    for n in range(10):
        go, reason, state = chain_control.decide(state, manifest, "r", 20 + n)
        assert go, reason                                         # zero means unlimited for web verification


def test_tor_installer_unpacks_the_expert_bundle(monkeypatch):
    import io, tarfile
    from webapp import tor_install
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("tor/tor.exe", b"MZ"), ("data/geoip", b"1,2,US")):
            info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
    listing = b'<a href="14.5.8/">14.5.8/</a> <a href="15.0a1/">15.0a1/</a> <a href="14.0.9/">14.0.9/</a>'
    seen = []

    def fake_get(url, timeout=120):
        seen.append(url)
        return listing if url == tor_install.BASE else buf.getvalue()
    monkeypatch.setattr(tor_install, "_get", fake_get)
    monkeypatch.setattr(tor_install, "IS_WINDOWS", True)
    ok, message = tor_install.install_tor(log=lambda m: None)
    assert ok, message
    assert any("14.5.8/tor-expert-bundle-windows-" in u for u in seen) and not any("15.0a1" in u for u in seen)
    assert (TMP / "tor" / "tor" / "tor.exe").is_file()
    from webapp.tor import find_tor
    assert find_tor("").endswith("tor.exe")
    import shutil; shutil.rmtree(TMP / "tor")


def test_tor_installer_rejects_path_traversal(monkeypatch):
    import io, tarfile
    from webapp import tor_install
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt"); info.size = 1; tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        tor_install._safe_extract(buf.getvalue(), TMP / "safe")


def test_progress_does_not_invent_a_measured_rate(client):
    (TMP / "results" / "run_manifest.json").write_text(json.dumps(
        {"from": "2026-10-19", "to": "2026-10-25", "unverified_pages": 1000, "stale_pages": 0}))
    data = client.get("/api/progress").get_json()
    assert data["pages_per_minute"] is None and data["measured"] is False and data["network"]
    assert client.get("/api/install-tor/status").get_json()["state"] in ("idle", "done", "failed", "running")


def test_live_progress_is_read_from_the_log_and_rate_is_measured(client):
    m = runner.manager()

    class FakeJob:
        meta = {}

        def save(self): pass
        def log(self, t): pass
    job = FakeJob(); job.meta = {}
    m._stamps.clear(); m._answered = 0
    for i, line in enumerate(["[US] [1/1506] US Only Took 5                        -> 18",
                              "[US] [2/1506] US Vauban Paris                       -> 29",
                              "[US] [3/1506] US Rose & Mabel                       -> unknown (zero not confirmed (Meta",
                              "[US] [4/1506] US Texas Sun Western Wear             -> 30"]):
        m._track(job, line + "\n")
    live = job.meta["live"]
    assert live["country"] == "US" and live["done"] == 4 and live["total"] == 1506
    assert live["answered"] == 3 and live["per_minute"] is not None      # "unknown" is not an answer
    m._track(job, "Meta is rate limiting us: cooling down 60s (cool-down 1/12 in a row)\n")
    assert "cool-down" in job.meta["live"]["note"]


def test_progress_api_uses_only_the_matching_reports_measured_speed(client, monkeypatch):
    (TMP / "results" / "run_manifest.json").write_text(json.dumps(
        {"from": "2026-10-19", "to": "2026-10-25", "unverified_pages": 900, "stale_pages": 0,
         "workflow_run_id": "r1", "verified_pages": 100, "pages_in_scope": 1000,
         "verification_percent": 10.0, "network": "Meta route recorded in report"}))
    class FakeManager:
        def current(self): return None
        def history(self, limit=30):
            return [{"id": "job-1", "network": "unrelated current setting",
                     "rounds": [{"run_id": "r1", "n": 2, "rate": 31.5}]},
                    {"id": "old-job", "rounds": [{"run_id": "old", "n": 4, "rate": 99.0}]}]
    monkeypatch.setattr(webapp_module, "manager", lambda: FakeManager())
    data = client.get("/api/progress").get_json()
    assert data["verified_pages"] == 100 and data["pages_in_scope"] == 1000 and data["verification_percent"] == 10.0
    assert data["pages_per_minute"] == 31.5 and data["measured"] is True
    assert data["network"] == "Meta route recorded in report"
    assert data["rate_job_id"] == "job-1" and data["rate_round"] == 2


def test_speed_profiles_use_light_pages_and_adaptive_cooldowns():
    from webapp.runner import JobManager
    for profile in ("direct", "tor", "proxy"):
        env = JobManager.build_env({"WH_API_KEY": "k", "WH_PROXY": "http://u:p@h:1"}, "verify", "r",
                                   {"countries": ["ES"]}, profile, "", "/tmp/m")
        assert env["WH_BLOCK_HEAVY"] == "1" and env["WH_ZERO_SETTLE"] == "3" and int(env["WH_VERIFY_WORKERS"]) == 4
        assert env["WH_VERIFY_SCOPE"] == "all" and env["WH_PROXY"] == ""
        assert env["WH_TOR_ROTATE_EVERY"] == "0" and env["WH_TOR_CIRCUITS"] == "1"
        assert env["WH_VERIFY_ROUNDS"] == "0" and env["WH_SWEEP_PASSES"] == "0"
        assert env["WH_BATCH_PARALLEL"] == "4" and env["WH_COUNTRY_WORKERS"] == "1"
        assert env["WH_PARALLEL_TAB_CAP"] == "5"


def test_country_worker_limit_is_bounded_to_five():
    from webapp.runner import JobManager
    six = {"countries": ["US", "GB", "FR", "ES", "NL", "PK"]}
    env = JobManager.build_env({"country_workers": 99, "verify_workers": 4}, "verify", "r", six, "direct", "", "/tmp/m")
    assert env["WH_COUNTRY_WORKERS"] == "5" and env["WH_VERIFY_WORKERS"] == "4"
    three = JobManager.build_env({"country_workers": 6, "verify_workers": 2}, "verify", "r",
                                 {"countries": ["US", "GB", "FR"]}, "direct", "", "/tmp/m")
    assert three["WH_COUNTRY_WORKERS"] == "3" and three["WH_VERIFY_WORKERS"] == "2"
    direct = JobManager.build_env({}, "verify", "r", {}, "direct", "", "/tmp/m")
    assert direct["WH_RATE_PAUSES"] == "0" and direct["WH_RATE_PAUSE_MAX"] == "3600"
    assert direct["WH_COUNTRY_WORKERS"] == "1" and direct["WH_VERIFY_WORKERS"] == "4"


def test_current_verifier_log_format_counts_rounded_numeric_answers_only(client):
    m = runner.manager()
    class FakeJob:
        meta = {}
        def save(self): pass
    job = FakeJob(); job.meta = {}
    m._stamps.clear(); m._country_stamps.clear(); m._answered = 0
    for line in ("[1/3] US One -> 10", "[2/3] US Two -> 0", "[3/3] US Three -> ~1,234"):
        m._track(job, line + "\n")
    assert job.meta["live"]["country"] == "US"
    assert job.meta["live"]["done"] == 3 and job.meta["live"]["total"] == 3
    assert job.meta["live"]["answered"] == 3 and job.meta["live"]["per_minute"] is not None


def test_competitor_api_searches_and_pages_the_full_deduplicated_report(client):
    import openpyxl
    path = TMP / "results" / "competitors_week_test.xlsx"
    headers = ["Ads Library link", "Site link", "Page name", "Score", "Verified active ads",
               "Verification", "Verification checked"]
    wb = openpyxl.Workbook()
    all_pages = wb.create_sheet("All Top")
    all_pages.append(headers)
    sample = None
    for number in range(1005):
        count = "~1,234" if number == 1004 else (None if number == 1000 else number)
        status = "unverified" if number == 1000 else "verified active"
        row = [f"https://www.facebook.com/ads/library/?country=US&view_all_page_id={7000 + number}",
               f"https://shop{number}.example", f"Page {number}", number, count, status, "2026-11-05T10:00:00"]
        all_pages.append(row)
        if number == 0:
            sample = row
    top = wb.create_sheet("Top")
    top.append(headers)
    assert sample is not None
    top.append(sample)                                           # duplicate summary row must not inflate totals
    wb.save(path)

    first = client.get("/api/competitors?limit=50&offset=0")
    assert first.status_code == 200 and first.get_json()["total"] == 1005
    assert len(first.get_json()["rows"]) == 50
    tail = client.get("/api/competitors?limit=50&offset=1000")
    assert tail.get_json()["total"] == 1005 and len(tail.get_json()["rows"]) == 5
    match = client.get("/api/competitors?q=Page+1004")
    record = match.get_json()["rows"][0]
    assert match.get_json()["total"] == 1 and record["active_ads"] == 1234 and record["approximate"] is True
    unverified = client.get("/api/competitors?status=unverified")
    assert unverified.get_json()["total"] == 1 and unverified.get_json()["rows"][0]["page_id"] == "8000"
    etag = first.headers["ETag"]
    unchanged = client.get("/api/competitors?limit=50&offset=0", headers={"If-None-Match": etag})
    assert unchanged.status_code == 304


def test_route_manifest_is_served_and_contains_page_routes(client):
    response = client.get("/manus-routes.json")
    assert response.status_code == 200 and response.is_json
    paths = {item["path"] for item in response.get_json()["routes"]}
    assert paths == {"/", "/runs/:job_id", "/reports", "/reports/:name", "/activity", "/settings"}


def test_activity_memory_shows_timestamped_actions_and_run_links(client, monkeypatch):
    class FakeManager:
        def history(self, limit=30):
            return [{"id": "job-memory", "status": "running", "params": {"mode": "verify", "countries": ["US"]},
                     "events": [{"at": "2026-11-05T13:00:00", "title": "Country report saved",
                                 "detail": "US: 1/2; partial countries: FR", "kind": "report"}]}]
    monkeypatch.setattr(webapp_module, "manager", lambda: FakeManager())
    page = client.get("/activity")
    assert page.status_code == 200 and b"PERSISTENT JOB JOURNAL" in page.data
    payload = client.get("/api/activity?limit=10").get_json()
    assert payload["events"][0]["at"] == "2026-11-05T13:00:00"
    assert payload["events"][0]["href"] == "/runs/job-memory"
    assert payload["events"][0]["countries"] == ["US"]


def test_run_form_uses_latest_saved_countries_and_verify_has_unlimited_all_page_defaults(client):
    (TMP / "results" / "run_manifest.json").write_text(json.dumps({"countries": ["DE", "SE"]}), encoding="utf-8")
    response = client.get("/")
    assert response.status_code == 200 and 'name="countries" value="DE,SE"' in response.get_data(as_text=True)
    params, error = runner.validate({"mode": "verify", "countries": "DE,SE"})
    assert not error and params is not None
    assert params["scope"] == "all" and params["max_minutes"] == 0 and params["auto_chain"] is True

def test_meta_api_is_off_and_not_called(client):
    data = client.post("/api/check-meta-token", data={"token": "EAAB-secret"}).get_json()
    assert data["ok"] is False and "off" in data["message"].lower()
    page = client.get("/settings").data
    assert b"META_API_TOKEN" not in page
    assert b"No Facebook login" in page
