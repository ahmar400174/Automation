"""WH Scanner web app  -  start:  python app.py   then open  http://127.0.0.1:8000"""
import hmac
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for

from webapp import settings as cfg
from webapp.paths import RESULTS, ROOT, RUNS

cfg.load_dotenv()
from webapp.runner import DEFAULT_MINUTES, manager, normalize_countries, validate  # noqa: E402  (after .env is loaded)
from webapp.tor import find_tor  # noqa: E402

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
_COMPETITOR_CACHE_LOCK = threading.Lock()
_COMPETITOR_CACHE: dict[str, Any] = {"key": None, "payload": None}


# ---------------------------------------------------------------- optional login (HTTP basic auth)
@app.before_request
def _auth():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        source = request.headers.get("Origin") or request.headers.get("Referer")
        parsed = urlparse(source) if source else None
        if not parsed or parsed.netloc.lower() != request.host.lower():
            return Response("Request origin is not trusted.", 403)
    password = os.environ.get("WH_WEB_PASSWORD", "") or cfg.load().get("web_password", "")
    if not password or request.endpoint in ("health",):
        host = os.environ.get("WH_HOST", "127.0.0.1").strip().lower()
        preview_only = os.environ.get("WH_ALLOW_UNAUTHENTICATED_PREVIEW") == "1"
        if request.endpoint != "health" and host not in ("127.0.0.1", "localhost", "::1") and not preview_only:
            return Response("Set a strong WH_WEB_PASSWORD before exposing this service.", 503)
        return None
    given = request.authorization
    if given and hmac.compare_digest(str(given.password or ""), password):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="WH Scanner"'})


def _safe_child(base, name):
    base = Path(base).resolve()
    target = (base / name).resolve()
    if base != target and base not in target.parents:
        abort(404)
    if not target.is_file():
        abort(404)
    return target


def _job_dir(job_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(job_id or "")):
        abort(404)
    base = Path(RUNS).resolve()
    target = (base / job_id).resolve()
    if target.parent != base:
        abort(404)
    return target


def _send(path, inline):
    return send_file(path, as_attachment=not inline, download_name=Path(path).name)


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    settings = cfg.load()
    default_countries = "US,FR,IT,ES,GB,NL"
    try:
        import json
        manifest = json.loads((RESULTS / "run_manifest.json").read_text(encoding="utf-8"))
        saved_countries = normalize_countries(manifest.get("countries"))
        if saved_countries:
            default_countries = ",".join(saved_countries)
    except (OSError, ValueError, AttributeError):
        pass
    return render_template("index.html", minutes=DEFAULT_MINUTES, default_countries=default_countries,
                           has_key=bool(settings.get("WH_API_KEY")),
                           tor_found=bool(find_tor(settings.get("tor_path", ""))),
                           use_tor=settings.get("use_tor"))


@app.route("/runs/<job_id>")
def run_page(job_id):
    path = _job_dir(job_id) / "job.json"
    if not path.exists():
        abort(404)
    import json
    return render_template("run.html", job=json.loads(path.read_text(encoding="utf-8")))


@app.route("/activity")
def activity_page():
    return render_template("activity.html")


@app.route("/reports")
def reports():
    return render_template("reports.html", items=_report_items())


def _report_items():
    items = []
    for path in sorted(RESULTS.glob("competitors_week_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.suffix in (".html", ".xlsx"):
            items.append({"name": path.name, "size_mb": round(path.stat().st_size / 1048576, 1),
                          "kind": path.suffix[1:], "mtime": path.stat().st_mtime,
                          "open_url": url_for("report_file", name=path.name, inline="1"),
                          "download_url": url_for("report_file", name=path.name),
                          "run_label": "Live report · updated as checkpoints complete"})
    live_root = Path(RESULTS) / "live_runs"
    if live_root.is_dir():
        for path in live_root.glob("*/competitors_week_*"):
            if path.suffix not in (".html", ".xlsx") or not path.is_file():
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(Path(RESULTS)).as_posix()
            except (OSError, ValueError):
                continue
            items.append({"name": path.name, "size_mb": round(stat.st_size / 1048576, 1),
                          "kind": path.suffix[1:], "mtime": stat.st_mtime,
                          "open_url": url_for("report_file", name=relative, inline="1"),
                          "download_url": url_for("report_file", name=relative),
                          "run_label": f"Live run snapshot · {path.parent.name}"})
    # Each completed round has its own immutable copy under its job record. Keep these visible
    # alongside the latest live snapshot so operators can compare runs day-by-day.
    for job in manager().history(1000):
        job_id = str(job.get("id") or "")
        for round_info in job.get("rounds") or []:
            round_no = round_info.get("n")
            if not job_id or not str(round_no or "").isdigit():
                continue
            for name in round_info.get("files") or []:
                if Path(str(name)).name != str(name) or Path(str(name)).suffix not in (".html", ".xlsx"):
                    continue
                target = Path(RUNS) / job_id / "files" / f"round_{round_no}" / str(name)
                try:
                    if not target.is_file():
                        continue
                    stat = target.stat()
                except OSError:
                    continue
                rel = f"round_{round_no}/{name}"
                mode = str(round_info.get("mode") or "run")
                items.append({"name": str(name), "size_mb": round(stat.st_size / 1048576, 1),
                              "kind": target.suffix[1:], "mtime": stat.st_mtime,
                              "open_url": url_for("job_file", job_id=job_id, name=rel, inline="1"),
                              "download_url": url_for("job_file", job_id=job_id, name=rel),
                              "run_label": f"Run {job_id} · round {round_no} · {mode}"})
    import datetime as dt
    for item in items:
        item["time"] = dt.datetime.fromtimestamp(item["mtime"]).strftime("%Y-%m-%d %H:%M")
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.route("/api/reports")
def api_reports():
    import datetime as dt
    items = _report_items()
    return jsonify(items=items, total=len(items), refreshed_at=dt.datetime.now().isoformat(timespec="seconds"))


@app.route("/reports/<path:name>")
def report_file(name):
    return _send(_safe_child(RESULTS, name), inline=request.args.get("inline") == "1")


@app.route("/files/<job_id>/<path:name>")
def job_file(job_id, name):
    return _send(_safe_child(_job_dir(job_id) / "files", name), inline=request.args.get("inline") == "1")


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    saved = False
    if request.method == "POST":
        form = request.form
        updates = {
            "WH_API_KEY": form.get("WH_API_KEY", "__keep__") or "__keep__",
            "fresh_hours": form.get("fresh_hours", 72),
            "use_tor": form.get("use_tor") == "on",
            "tor_failover": form.get("tor_failover") == "on",
            "tor_path": form.get("tor_path", ""),
            "run_tests": form.get("run_tests") == "on",
            "tor_circuits": 1,
            "verify_workers": form.get("verify_workers", ""),
            "verify_gap": form.get("verify_gap", ""),
            "country_workers": form.get("country_workers", ""),
            "web_password": form.get("web_password", "") or "__keep__",
        }
        if form.get("clear_key") == "on":
            updates["WH_API_KEY"] = ""
        if form.get("clear_password") == "on":
            updates["web_password"] = ""
        cfg.save(updates)
        saved = True
    s = cfg.load()
    return render_template("settings.html", s=s, saved=saved, key_mask=cfg.masked(s["WH_API_KEY"]),
                           pw_set=bool(os.environ.get("WH_WEB_PASSWORD") or s["web_password"]),
                           password_from_env=bool(os.environ.get("WH_WEB_PASSWORD")),
                           tor_found=find_tor(s.get("tor_path", "")))


# ---------------------------------------------------------------- JSON api used by the pages
@app.route("/api/start", methods=["POST"])
def api_start():
    params, error = validate(request.form if request.form else (request.get_json(silent=True) or {}))
    if error:
        return jsonify(ok=False, error=error), 400
    job_id, error = manager().start(params)
    if error:
        return jsonify(ok=False, error=error), 409
    return jsonify(ok=True, job_id=job_id)


@app.route("/api/check-meta-token", methods=["POST"])
def api_check_meta_token():
    """Kept so old pages do not 404. The API is not used."""
    return jsonify(ok=False, message="Meta API is off. Counts are read from the Ad Library website. No Facebook login is used.")


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify(ok=manager().stop())


@app.route("/api/state")
def api_state():
    m = manager()
    return jsonify(running=m.running(), current=m.current(), history=m.history(15))


@app.route("/api/activity")
def api_activity():
    """Persistent, chronological job memory; legacy jobs receive basic synthesized milestones."""
    import datetime as dt
    try:
        limit = min(1000, max(1, int(request.args.get("limit", "200"))))
    except ValueError:
        return jsonify(error="limit must be an integer from 1 to 1000."), 400
    items = []
    for job in manager().history(1000):
        params = job.get("params") or {}
        events = list(job.get("events") or [])
        if not events:
            events.append({"at": job.get("started") or "", "title": "Job " + str(job.get("status") or "recorded"),
                           "detail": str(job.get("message") or "Legacy job record."), "kind": "job"})
            for round_info in job.get("rounds") or []:
                events.append({"at": round_info.get("started") or "", "title": f"Round {round_info.get('n')} started",
                               "detail": str(round_info.get("mode") or ""), "kind": "round"})
                if round_info.get("ended"):
                    events.append({"at": round_info.get("ended"), "title": f"Round {round_info.get('n')} finished",
                                   "detail": "Report saved." if round_info.get("report") else "No report saved.",
                                   "kind": "report"})
        for event in events:
            items.append({**event, "job_id": job.get("id", ""), "status": job.get("status", "unknown"),
                          "mode": params.get("mode", ""), "countries": params.get("countries", []),
                          "href": "/runs/" + str(job.get("id", ""))})
    items.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return jsonify(events=items[:limit], total=len(items), generated_at=dt.datetime.now().isoformat(timespec="seconds"))


@app.route("/api/progress")
def api_progress():
    """Return verified totals and only a rate actually measured by completed work."""
    import json
    try:
        m = json.loads((RESULTS / "run_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify(known=False)
    if not isinstance(m, dict) or m.get("unverified_pages") is None:
        return jsonify(known=False)
    manager_instance = manager()
    history = manager_instance.history(30)
    network = str(m.get("network") or "").strip()
    rate, measured, rate_job_id, rate_round = None, False, None, None
    manifest_run_id = str(m.get("workflow_run_id") or "")
    for job in history:
        for rnd in reversed(job.get("rounds") or []):
            if manifest_run_id and rnd.get("run_id") == manifest_run_id:
                if not network:
                    network = str(job.get("network") or "").strip()
                try:
                    rate_value = float(rnd.get("rate"))
                except (TypeError, ValueError):
                    rate_value = None
                if rate_value is not None and rate_value > 0:
                    rate, measured = rate_value, True
                    rate_job_id, rate_round = job.get("id"), rnd.get("n")
                break
        if measured:
            break
    if not network:
        current = manager_instance.current()
        current_rounds = (current or {}).get("rounds") or []
        if (current and str(current.get("status") or "") in ("starting", "running")
                and any(rnd.get("run_id") == manifest_run_id for rnd in current_rounds)):
            network = str(current.get("network") or "").strip()
    network = network or "Network route not recorded for this report"
    return jsonify(known=True, unverified=m.get("unverified_pages"), stale=m.get("stale_pages") or 0,
                   failed=m.get("verification_failed") or 0, date_from=m.get("from"), date_to=m.get("to"),
                   outcome=m.get("outcome"), network=network, pages_per_minute=rate, measured=measured,
                   verified_pages=m.get("verified_pages"), pages_in_scope=m.get("pages_in_scope"),
                   verification_percent=m.get("verification_percent"), rate_job_id=rate_job_id,
                   rate_round=rate_round)


@app.route("/api/competitors")
def api_competitors():
    """Read the latest persisted merged workbook; never invent rows or counts."""
    import hashlib
    import openpyxl
    import urllib.parse
    from verify_active import adlib_url

    try:
        limit = min(200, max(1, int(request.args.get("limit", "50"))))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        return jsonify(known=False, error="limit and offset must be non-negative integers."), 400
    query = request.args.get("q", "").strip().casefold()[:160]
    status_filter = request.args.get("status", "all").strip().lower()
    if status_filter not in ("all", "verified", "inactive", "unverified", "stale", "failed"):
        return jsonify(known=False, error="Unknown verification status filter."), 400

    files = sorted(RESULTS.glob("competitors_week_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return jsonify(known=False, rows=[], total=0, report=None, source="Meta Ad Library")
    stat = files[0].stat()
    cache_key = (str(files[0].resolve()), stat.st_mtime_ns, stat.st_size)
    with _COMPETITOR_CACHE_LOCK:
        payload = _COMPETITOR_CACHE["payload"] if _COMPETITOR_CACHE["key"] == cache_key else None
    workbook = None
    try:
        if payload is None:
            workbook = openpyxl.load_workbook(files[0], read_only=True, data_only=True)
            rows_by_page: dict[tuple[str, str], dict[str, Any]] = {}
            for sheet_name in workbook.sheetnames:
                if sheet_name.strip().lower() not in ("top", "others", "all top", "all others"):
                    continue
                sheet = workbook[sheet_name]
                values = sheet.iter_rows(values_only=True)
                headers = next(values, None)
                if not headers:
                    continue
                columns = {str(value).strip(): index for index, value in enumerate(headers) if value}

                def value(record, key):
                    pos = columns.get(key)
                    return record[pos] if pos is not None and pos < len(record) else None

                for record in values:
                    page_link = str(value(record, "Ads Library link") or "")
                    found_id = re.search(r"[?&]view_all_page_id=(\d+)", page_link)
                    if not found_id:
                        continue
                    query_args = urllib.parse.parse_qs(urllib.parse.urlparse(page_link).query)
                    country = str((query_args.get("country") or ["ALL"])[0]).upper()
                    page_id = found_id.group(1)
                    status = str(value(record, "Verification") or "Unverified").strip()
                    count_text = str(value(record, "Verified active ads") or "").strip()
                    count_match = re.fullmatch(r"(~)?([\d,]+)", count_text)
                    count_num = int(count_match.group(2).replace(",", "")) if count_match else None
                    site_url = str(value(record, "Site link") or "").strip()
                    parsed_site = urlparse(site_url if "://" in site_url else "https://" + site_url)
                    if parsed_site.scheme not in ("http", "https") or not parsed_site.netloc:
                        site_url = ""
                    row = {
                        "page_id": page_id, "country": country, "page_name": str(value(record, "Page name") or ""),
                        "site_url": site_url, "site": (parsed_site.hostname or "") if site_url else "",
                        "active_ads": count_num, "approximate": bool(count_match and count_match.group(1)),
                        "status": status, "checked": str(value(record, "Verification checked") or ""),
                        "score": value(record, "Score"), "ad_library_url": adlib_url(page_id, country),
                    }
                    page_key = (country, page_id)
                    previous = rows_by_page.get(page_key)
                    row_answered = row["active_ads"] is not None
                    previous_answered = previous is not None and previous.get("active_ads") is not None
                    newer_check = previous is not None and str(row.get("checked") or "") > str(previous.get("checked") or "")
                    if previous is None or (row_answered and not previous_answered) or (
                            row_answered == previous_answered and newer_check):
                        rows_by_page[page_key] = row
            rows = list(rows_by_page.values())
            rows.sort(key=lambda row: (row["active_ads"] is None, -(row["active_ads"] or 0), row["site"].lower()))
            payload = {"known": True, "rows": rows, "report": files[0].name, "source": "Meta Ad Library"}
            with _COMPETITOR_CACHE_LOCK:
                _COMPETITOR_CACHE.update(key=cache_key, payload=payload)

        def matches_status(row):
            raw = str(row.get("status") or "unverified").lower()
            category = ("failed" if "failed" in raw or "error" in raw else
                        "inactive" if "inactive" in raw else
                        "stale" if "stale" in raw else
                        "unverified" if "unverified" in raw else
                        "verified" if "verified" in raw else "unverified")
            return status_filter == "all" or category == status_filter

        filtered = [row for row in payload["rows"] if matches_status(row) and
                    (not query or query in " ".join(str(row.get(key) or "")
                    for key in ("page_name", "site", "site_url", "page_id")).casefold())]
        response = jsonify(known=True, rows=filtered[offset:offset + limit], total=len(filtered),
                           offset=offset, limit=limit, report=payload["report"], source=payload["source"])
        etag = hashlib.sha256(repr((cache_key, query, status_filter, offset, limit)).encode()).hexdigest()[:32]
        response.set_etag(etag)
        response.headers["Cache-Control"] = "private, no-cache"
        return response.make_conditional(request)
    except Exception as exc:
        app.logger.exception("Could not read latest competitor workbook")
        return jsonify(known=False, rows=[], total=0, report=files[0].name,
                       error=f"Could not read the latest report ({type(exc).__name__}).")
    finally:
        if workbook:
            workbook.close()


TOR_INSTALL = {"state": "idle", "message": ""}


@app.route("/api/install-tor", methods=["POST"])
def api_install_tor():
    import threading
    from webapp.tor_install import install_tor
    if TOR_INSTALL["state"] == "running":
        return jsonify(ok=False, message="Already installing...")
    TOR_INSTALL.update(state="running", message="Downloading Tor (about 15 MB) ...")

    def work():
        try:
            ok, message = install_tor(log=lambda m: TOR_INSTALL.update(message=m))
        except Exception as exc:                       # never leave the button stuck
            ok, message = False, f"{type(exc).__name__}: {exc}"
        TOR_INSTALL.update(state="done" if ok else "failed", message=message)
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, message=TOR_INSTALL["message"])


@app.route("/api/install-tor/status")
def api_install_tor_status():
    return jsonify(dict(TOR_INSTALL, found=bool(find_tor(cfg.load().get("tor_path", "")))))


@app.route("/api/job/<job_id>")
def api_job(job_id):
    path = _job_dir(job_id) / "job.json"
    if not path.exists():
        abort(404)
    import json
    for _ in range(5):                                  # Windows: the file may be replaced at this very moment
        try:
            return jsonify(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            import time
            time.sleep(0.1)
    abort(503)


@app.route("/api/log/<job_id>")
def api_log(job_id):
    path = _job_dir(job_id) / "run.log"
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    if not path.exists():
        return jsonify(text="", offset=0)
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if offset > size:
            offset = 0
        if size - offset > 200_000:                    # a page load of a huge log: show only the tail
            offset = size - 200_000
        handle.seek(offset)
        data = handle.read(400_000)
    return jsonify(text=data.decode("utf-8", "replace"), offset=offset + len(data))


@app.route("/health")
def health():
    return jsonify(ok=True)


@app.route("/manus-routes.json")
def manus_routes():
    return send_file(Path(__file__).resolve().parent / "public" / "manus-routes.json", mimetype="application/json")


def _free_port(start):
    import socket
    for port in range(start, start + 60):
        with socket.socket() as sock:
            try:
                sock.bind((os.environ.get("WH_HOST", "127.0.0.1"), port))
                return port
            except OSError:
                continue
    return start


def _after_start(url):
    """UPDATED LOGIC (one-click start): open the browser and, when WH_AUTORUN is set, start the workflow by itself."""
    import threading
    import time

    def work():
        time.sleep(2.5)
        if os.environ.get("WH_OPEN_BROWSER") == "1":
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        mode = (os.environ.get("WH_AUTORUN") or "").strip().lower()
        if mode:
            params, error = validate({"mode": mode, "auto_chain": "1",
                                      "countries": os.environ.get("WH_AUTORUN_COUNTRIES", "US,FR,IT,ES,GB,NL"),
                                      "date_from": os.environ.get("WH_AUTORUN_FROM", ""),
                                      "date_to": os.environ.get("WH_AUTORUN_TO", "")})
            if error:
                print(f"Auto-run not started: {error}", flush=True)
                return
            job_id, error = manager().start(params)
            print(f"Auto-run started: job {job_id}" if job_id else f"Auto-run not started: {error}", flush=True)
    threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("WH_HOST", "127.0.0.1")
    port = int(os.environ["WH_PORT"]) if os.environ.get("WH_PORT") else _free_port(8000)   # busy port -> next free one
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{port}"
    print(f"\n  WH Scanner is running:  {url}\n  Working folder: {ROOT}\n  Stop with CTRL+C\n", flush=True)
    _after_start(url)
    try:
        import importlib
        server = importlib.import_module("waitress")
        server.serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
