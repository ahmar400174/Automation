"""Runs the workflow steps of the old GitHub Action on this computer / server, one job at a time.

A job = what one click on "Run workflow" did on GitHub:
    [Tor] -> tests -> purge legacy zeros -> clean workspace -> run_weekly.py -> save scan progress
    -> collect this run's files -> chain decision -> (next automatic "verify" round) ...
Everything is written to data/runs/<job id>/ (job.json, run.log, files/round_N/...).
"""
import datetime as dt
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from . import settings as cfg
from .paths import APP_DIR, DATA, ROOT, RESULTS, RUNS, atomic_write
from .tor import TorFarm, find_tor, running_socks

import chain_control  # noqa: E402  (same decision logic the workflow used)

MODES = ("estimate", "scan", "rebuild", "verify")
DEFAULT_MINUTES = {"scan": 360, "rebuild": 60, "verify": 0, "estimate": 30}
RUN_SCRIPT = Path(os.environ.get("WH_RUN_SCRIPT") or (ROOT / "run_weekly.py"))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIVE_RE = re.compile(r"^(?:\[(?P<country_prefix>[A-Z]{2})\]\s+)?\[(?P<done>\d+)/(?P<total>\d+)\]\s+(?:(?P<country_body>[A-Z]{2})\s+)?(?P<name>.*?)\s+->\s*(?P<answer>.*)$")
BATCH_RE = re.compile(r"^(?:\[([A-Z]{2})\]\s+)?BATCH\s+(\d+)/(\d+)\s+(\d+)/(\d+)\s+(\w+)")
ANSWER_RE = re.compile(r"^~?[\d,]+$")


def normalize_countries(value):
    """'US, FR' / ['US','FR'] / 'U,S,,,F,R' (letter-split) -> ['US','FR']."""
    if isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    tokens = [t for t in re.split(r"[\s,;|]+", str(value or "").upper()) if t]
    if tokens and all(len(t) == 1 for t in tokens):
        joined = "".join(tokens)
        tokens = [joined[i:i + 2] for i in range(0, len(joined), 2)]
    out = []
    for token in tokens:
        token = re.sub(r"[^A-Z]", "", token)
        if len(token) == 2 and token not in out:
            out.append(token)
    return out


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def validate(form):
    """Returns (params, error)."""
    mode = str(form.get("mode") or "").strip().lower()
    if mode not in MODES:
        return None, "Unknown mode."
    countries = normalize_countries(form.get("countries") or "US,FR,IT,ES,GB,NL")
    if not countries:
        return None, "Give at least one country (example: US,FR,IT,ES,GB,NL)."
    if len(countries) > 30:
        return None, "Choose no more than 30 countries per run."
    dates = {}
    for key in ("date_from", "date_to"):
        value = str(form.get(key) or "").strip()
        if value and not DATE_RE.match(value):
            return None, "Dates must look like 2026-10-19 (or stay empty = last week)."
        if value:
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                return None, "Enter real calendar dates in YYYY-MM-DD format."
        dates[key] = value
    if dates["date_from"] and dates["date_to"] and dates["date_from"] > dates["date_to"]:
        return None, "The start date must be on or before the end date."
    try:
        max_calls = int(form.get("max_calls") or 5000)
        minutes = int(form.get("max_minutes") or DEFAULT_MINUTES[mode])
    except ValueError:
        return None, "Max calls / minutes must be numbers."
    if not (1 <= max_calls <= 100000) or not (0 <= minutes <= 2000):
        return None, "Max calls must be 1-100000 and round minutes 0-2000."
    if minutes == 0 and mode != "verify":
        return None, "A no-deadline round (0 minutes) is available only for verify mode."
    scope = str(form.get("scope") or "all").strip().lower()
    if scope not in ("listed", "all"):
        return None, "Choose either all imported pages or reportable store-linked pages."
    pause_raw = str(form.get("pause_minutes") or "").strip()
    if pause_raw and not (pause_raw.isdigit() and int(pause_raw) <= 600):
        return None, "Pause between rounds must be a number of minutes (0-600) or empty."
    truthy = lambda v: str(v).lower() in ("1", "true", "on", "yes")
    params = {
        "mode": mode, "countries": countries, "date_from": dates["date_from"], "date_to": dates["date_to"],
        "scope": scope,
        "max_calls": max_calls, "max_minutes": minutes,
        "pause_minutes": int(pause_raw) if pause_raw else None,           # UPDATED LOGIC: set on the Run page
        "resume": truthy(form.get("resume", "")), "keep_all": truthy(form.get("keep_all", "")),
        "auto_chain": truthy(form.get("auto_chain", "1" if mode != "estimate" else "")) and mode != "estimate",
    }
    return params, ""


class Job:
    def __init__(self, params):
        self.id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.dir = RUNS / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = {"id": self.id, "status": "starting", "params": params, "started": now_iso(), "ended": "",
                     "rounds": [], "events": [], "message": "", "network": ""}
        self.event("Job accepted", "Workflow started; new requests are not placed in a hidden queue.")
        self.save()

    def save(self):
        atomic_write(self.dir / "job.json", json.dumps(self.meta, indent=2, ensure_ascii=False))  # UPDATED LOGIC: Windows-safe

    def event(self, title, detail="", **fields):
        events = self.meta.setdefault("events", [])
        events.append({"at": now_iso(), "title": str(title)[:120], "detail": str(detail)[:500], **fields})
        del events[:-500]
        self.save()

    @property
    def logfile(self):
        return self.dir / "run.log"

    def log(self, text):
        with open(self.logfile, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.thread = None
        self.proc = None
        self.stop_flag = threading.Event()
        self.tor = None
        self._stamps = deque(maxlen=400)       # times of the latest Meta checks (speed = checks per minute)
        self._country_stamps = {}
        self._answered = 0
        self._last_save = 0.0
        self._mark_interrupted()

    # ------------------------------------------------------------ state
    def _mark_interrupted(self):
        for path in RUNS.glob("*/job.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("status") in ("starting", "running"):
                meta["status"] = "interrupted"
                meta["message"] = "The app was closed while this job was running."
                meta["ended"] = meta.get("ended") or now_iso()
                atomic_write(path, json.dumps(meta, indent=2))

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def current(self):
        return self.job.meta if self.job else None

    @staticmethod
    def history(limit=30):
        out = []
        for path in RUNS.glob("*/job.json"):
            try:
                out.append((path.stat().st_mtime_ns, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        out.sort(key=lambda item: (item[0], str(item[1].get("started") or ""),
                                   str(item[1].get("id") or "")), reverse=True)
        return [job for _mtime, job in out[:max(0, int(limit))]]

    # ------------------------------------------------------------ control
    def start(self, params):
        with self.lock:
            if self.running():
                return None, "A job is already running. This request was not queued; wait for completion or stop the active run."
            settings = cfg.load()
            if params["mode"] in ("estimate", "scan") and not settings.get("WH_API_KEY"):
                return None, "WH_API_KEY is missing: open Settings and paste the WinningHunter API key."
            if not RUN_SCRIPT.exists():
                return None, f"{RUN_SCRIPT.name} not found in {ROOT}."
            self.stop_flag.clear()
            self.job = Job(params)
            self.thread = threading.Thread(target=self._run_job, args=(self.job, settings), daemon=True)
            self.thread.start()
            return self.job.id, ""

    def stop(self):
        if not self.running():
            return False
        self.stop_flag.set()
        self._kill_proc()
        return True

    def _kill_proc(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------ one process, logged
    @staticmethod
    def _event(job, title, detail="", **fields):
        add = getattr(job, "event", None)
        if callable(add):
            add(title, detail, **fields)
            return
        events = job.meta.setdefault("events", [])
        events.append({"at": now_iso(), "title": title, "detail": detail, **fields})
        if hasattr(job, "save"):
            job.save()

    def _track(self, job, line):
        """UPDATED LOGIC: live progress for the dashboard, read from the run log (no extra Meta calls)."""
        text = line.rstrip("\n")
        batch = BATCH_RE.match(text)
        if batch:
            live = job.meta.get("live") or {}
            country = batch.group(1) or live.get("country") or "ALL"
            bi, bn = int(batch.group(2)), int(batch.group(3))
            bdone, btotal, state = int(batch.group(4)), int(batch.group(5)), batch.group(6)
            by: dict[str, dict[str, Any]] = job.meta.setdefault("by_country", {})
            row: dict[str, Any] = dict(by.get(country) or {"done": 0, "total": 0, "remaining": 0,
                                                           "per_minute": None, "note": ""})
            batches: dict[str, Any] = dict(row.get("batches") or {})
            batches[str(bi)] = {"n": bi, "of": bn, "done": bdone, "total": btotal, "state": state}
            row["batches"] = batches
            by[country] = row
            live = dict(job.meta.get("live") or {})
            live["country"] = country
            live["by_country"] = by
            job.meta["by_country"] = by
            job.meta["live"] = live
            if state.lower() in ("done", "finished", "complete") and bi == bn and bdone >= btotal:
                self._event(job, "Country batch finished", f"{country}: final batch {bi}/{bn} ({bdone}/{btotal}).",
                            country=country, kind="country")
            self._maybe_save(job)
            return
        match = LIVE_RE.match(text)
        if not match:
            if "cooling down" in text.lower() or "rate limiting" in text.lower():
                live = job.meta.setdefault("live", {})
                note = "Meta is rate limiting requests; the verifier is in a cool-down before retrying the same pages."
                if live.get("note") != note:
                    self._event(job, "Meta cooldown", note, kind="cooldown")
                live["note"] = note
                self._maybe_save(job, force=True)
            if "COUNTRY CHECKPOINT saved:" in text:
                self._event(job, "Country report saved", text.split("COUNTRY CHECKPOINT saved:", 1)[1].strip(), kind="report")
            return
        country = match.group("country_prefix") or match.group("country_body") or "ALL"
        done_n, total, answer = int(match.group("done")), int(match.group("total")), match.group("answer").strip()
        now = time.time()
        by = job.meta.setdefault("by_country", {})
        prev = by.get(country) or {}
        progressed = done_n > int(prev.get("done") or 0)
        answered = progressed and ANSWER_RE.fullmatch(answer) is not None
        if answered:
            self._stamps.append(now)
            self._country_stamps.setdefault(country, deque(maxlen=400)).append(now)
            self._answered += 1
        recent = [t for t in self._stamps if now - t <= 120]
        span = max(5.0, now - recent[0]) if recent else 5.0
        rate = round(len(recent) * 60.0 / span, 1) if len(recent) >= 3 else None
        stamps = self._country_stamps.setdefault(country, deque(maxlen=400))
        crecent = [t for t in stamps if now - t <= 120]
        cspan = max(5.0, now - crecent[0]) if crecent else 5.0
        crate = round(len(crecent) * 60.0 / cspan, 1) if len(crecent) >= 3 else None
        batches = dict(prev.get("batches") or {})
        by[country] = {"done": done_n, "total": total, "remaining": max(0, total - done_n),
                       "per_minute": crate, "note": "", "batches": batches}
        job.meta["live"] = {"country": country, "done": done_n, "total": total, "per_minute": rate,
                            "answered": self._answered, "note": "", "by_country": by}
        if progressed and total > 0 and done_n >= total:
            self._event(job, "Verification batch progress", f"{country}: {done_n}/{total} in this batch.",
                        country=country, kind="country")
        self._maybe_save(job)

    def _maybe_save(self, job, force=False):
        now = time.time()
        if force or now - self._last_save >= 4:
            self._last_save = now
            job.save()

    def _exec(self, job, cmd, env, label, timeout=None, track=False):
        job.log(f"\n$ {label}")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200       # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, **kwargs)
            self.proc = proc
        except OSError as exc:
            job.log(f"could not start: {exc}")
            return 127
        if proc.stdout is None:
            self.proc = None
            job.log("could not capture workflow output")
            return 127
        with open(job.logfile, "a", encoding="utf-8", errors="replace") as handle:
            for line in proc.stdout:
                handle.write(line)
                handle.flush()
                if track:
                    self._track(job, line)
        code = proc.wait()
        self.proc = None
        return code

    # ------------------------------------------------------------ environment (= the workflow's env block)
    @staticmethod
    def build_env(settings, mode, run_id, params, profile, tor_ports, marker):
        env = dict(os.environ)
        tor = profile in ("tor", "failover")
        try:
            workers = max(1, min(4, int(settings.get("verify_workers") or 4)))
        except (TypeError, ValueError):
            workers = 4
        try:
            gap = max(0.25, min(10.0, float(settings.get("verify_gap") or 0.75)))
        except (TypeError, ValueError):
            gap = 0.75
        try:
            country_workers = max(1, min(5, int(settings.get("country_workers") or 5)))
        except (TypeError, ValueError):
            country_workers = 5
        # Allocate the five-tab budget against this run's actual country count, not the configured
        # maximum. Previously a one-country run inherited the six-country cap and was stuck at one tab.
        selected = params.get("countries") or []
        country_count = len(selected) if isinstance(selected, (list, tuple, set)) else len(
            [part for part in str(selected).replace(";", ",").split(",") if part.strip()])
        country_count = max(1, country_count)
        active_country_workers = min(country_workers, country_count, 5)
        env.update({
            "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "GITHUB_RUN_ID": run_id,                      # run_weekly.py stamps it into the manifest
            "WH_API_KEY": settings.get("WH_API_KEY", ""),
            "WH_PROXY": "",                              # no paid or rotating proxy in the $0 build
            "WH_TOR_PORTS": tor_ports if tor else "",
            "WH_TOR_FAILOVER": "1" if profile == "failover" else "0",
            "WH_NETWORK_PROFILE": ("Meta: direct, Tor failover on rate limit" if profile == "failover"
                                   else "Meta: Tor (one steady route; no exit rotation)" if tor
                                   else "Meta: direct network route"),
            "WH_BLOCK_MARKER": str(marker),
            "WH_VERIFY_LIVE": "0" if mode == "estimate" else "1",
            "WH_VERIFY_LIMIT": "0",
            "WH_PROXY_ROTATE": "0", "WH_POLL_MS": "250", "WH_THUMB_WORKERS": "8",
            "WH_STRICT": "0", "WH_ZERO_SETTLE": "3",
            "WH_BLOCK_HEAVY": "1",                         # no pictures / video / fonts while reading the counter
            "WH_VERIFY_ROUND_WAIT": "60",
            "WH_VERIFY_FRESH_HOURS": str(int(settings.get("fresh_hours") or 72)),
            "META_API_TOKEN": "",
            "WH_KEEP_ALL_STORES": "1" if params.get("keep_all") else "0",
            "WH_TOR_ROTATE_EVERY": "0",
            "WH_TOR_WAIT_SECONDS": "0",
            "WH_TOR_CIRCUITS": "1",
            "WH_VERIFY_SCOPE": params.get("scope", "all"),
            "WH_VERIFY_WORKERS": str(workers),
            "WH_VERIFY_GAP": str(gap),
            "WH_RATE_PAUSES": "0",
            "WH_RATE_PAUSE_SECONDS": "60",
            "WH_RATE_PAUSE_MAX": "3600",
            "WH_VERIFY_ROUNDS": "0",
            "WH_SWEEP_PASSES": "0",
            "WH_PARALLEL_TAB_CAP": "5",
            "WH_PARALLEL_COUNTRIES": "1",
            "WH_COUNTRY_WORKERS": str(active_country_workers),
            "WH_BATCH_PARALLEL": str(workers),
            "WH_BATCH_SIZE": "150",
        })
        return env

    # ------------------------------------------------------------ the job
    def _run_job(self, job, settings):
        params = job.meta["params"]
        job.meta["status"] = "running"
        job.event("Job running", "Worker accepted the job and began workflow setup.", kind="job")
        job.save()
        mode = params["mode"]
        countries = list(params["countries"])
        date_from, date_to = params["date_from"], params["date_to"]
        budget = int(params.get("max_minutes") or 0)
        # "Max minutes" is the budget of the WHOLE job (every automatic round, test run and pause together),
        # not of each round. 0 = no limit.
        params["deadline_ts"] = time.time() + budget * 60 if budget > 0 else 0
        state = chain_control.fresh_state(time.time())
        chain_control._write(RESULTS / "chain_state.json", state)
        try:
            profile, ports = "direct", ""
            if mode != "estimate":
                if settings.get("WH_PROXY"):
                    job.log("Legacy proxy setting detected; it is intentionally ignored in this free, non-rotating build.")
                if settings.get("use_tor") or settings.get("tor_failover"):
                    tor_exe = find_tor(settings.get("tor_path", ""))
                    if tor_exe:
                        job.log(f"Starting one steady Tor route ({tor_exe}); exit rotation is disabled.")
                        try:
                            self.tor = TorFarm(tor_exe, log=job.log, circuits=1)
                            ready = self.tor.start(countries)
                        except Exception as exc:
                            job.log(f"Tor did not start ({type(exc).__name__}: {exc}). "
                                    f"Checking Meta without Tor.")
                            if self.tor:
                                try:
                                    self.tor.stop()
                                except Exception:
                                    pass
                            self.tor = None
                            ready = {}
                        if self.tor and any(ready.values()):
                            profile = "tor" if settings.get("use_tor") else "failover"
                            ports = self.tor.env_value()
                    else:
                        port = running_socks()
                        if port:
                            job.log(f"Tor program not installed, but a running Tor was found on port {port}: using it.")
                            ports = ",".join(f"{str(c).upper()}:{port}:0" for c in countries)
                            profile = "tor" if settings.get("use_tor") else "failover"
                        else:
                            job.log("TOR NOT FOUND: failover cannot work. Open Settings and press Install Tor "
                                    "(or start Tor Browser and keep it open), then run again. "
                                    "Using the direct route with normal pauses.")
            job.meta["network"] = {"tor": "Tor (one steady circuit; no exit rotation)",
                                   "failover": "direct, Tor failover on rate limit",
                                   "direct": "direct network route"}[profile]
            if profile in ("tor", "failover"):
                job.log("Meta API is off; public Ad Library pages are checked without Facebook login or identity.")
            job.save()
            job.event("Network ready", job.meta["network"], kind="network")

            round_no = 0
            while True:
                round_no += 1
                round_mode = mode if round_no == 1 else "verify"
                result = self._one_round(job, settings, round_no, round_mode, params, countries,
                                         date_from, date_to, profile, ports)
                if self.stop_flag.is_set():
                    job.meta["status"], job.meta["message"] = "stopped", "Stopped by you."
                    break
                if round_mode == "estimate" or not params.get("auto_chain"):
                    job.meta["status"] = "finished" if result["report"] or round_mode == "estimate" else "failed"
                    break
                manifest = result["manifest"]
                left_s = params["deadline_ts"] - time.time() if params.get("deadline_ts") else None
                if left_s is not None and left_s < 240:
                    job.log(f"\nTime budget of {params['max_minutes']} min is used: no further automatic round.")
                    job.meta["status"] = "finished" if result["report"] else "failed"
                    job.meta["message"] = f"Stopped at the {params['max_minutes']} min time budget."
                    break
                go, reason, state = chain_control.decide(
                    chain_control._read(RESULTS / "chain_state.json"), manifest, result["run_id"], time.time(),
                    max_hours=0,
                    max_rounds=0,
                    max_stalls=0)
                chain_control._write(RESULTS / "chain_state.json", state)
                job.log(f"\nchain decision: {reason}")
                job.meta["message"] = reason
                if not go:
                    job.meta["status"] = "finished" if result["report"] else "failed"
                    break
                date_from, date_to = manifest.get("from", ""), manifest.get("to", "")
                countries = normalize_countries(manifest.get("countries")) or countries
                pause = settings.get("chain_pause_seconds")
                pause = int(pause) if str(pause).isdigit() else 60
                if params.get("pause_minutes") is not None:              # the Run page value wins
                    pause = int(params["pause_minutes"]) * 60
                stalls = int(state.get("stalls") or 0)
                if stalls:
                    pause = min(3600, max(pause, 300) * (1 + min(stalls, 10)))
                    job.log("No new answers this round; waiting longer before retrying the same pages.")
                if left_s is not None:
                    pause = int(max(0, min(pause, left_s - 240)))          # a pause never eats the next round
                job.log(f"Next automatic verify round in {pause}s ...")
                for _ in range(pause):
                    if self.stop_flag.is_set():
                        break
                    time.sleep(1)
                if self.stop_flag.is_set():
                    job.meta["status"], job.meta["message"] = "stopped", "Stopped by you."
                    break
        except Exception as exc:                                      # never leave a job "running" forever
            job.log(f"\nINTERNAL ERROR: {type(exc).__name__}: {exc}")
            job.meta["status"], job.meta["message"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if self.tor:
                self.tor.stop()
                self.tor = None
            job.meta["ended"] = now_iso()
            job.event("Job " + str(job.meta.get("status") or "finished"),
                      str(job.meta.get("message") or "Workflow ended."), kind="job")
            job.save()

    def _one_round(self, job, settings, round_no, mode, params, countries, date_from, date_to, profile, ports):
        run_id = f"{job.id}-r{round_no}"
        rinfo = {"n": round_no, "mode": mode, "run_id": run_id, "started": now_iso(), "ended": "",
                 "exit": None, "report": False, "files": [], "summary": {}}
        job.meta["rounds"].append(rinfo)
        job.event(f"Round {round_no} started", f"{mode}; countries={','.join(countries)}; period={date_from}..{date_to}.",
                  kind="round", round=round_no)
        job.save()
        job.log(f"\n{'=' * 70}\nROUND {round_no}: mode={mode} from='{date_from}' to='{date_to}' "
                f"countries={','.join(countries)} network={job.meta['network']}\n{'=' * 70}")
        marker = job.dir / f"meta_blocked_r{round_no}"
        env = self.build_env(settings, mode, run_id, params, profile, ports, marker)
        env["COUNTRIES"] = ",".join(countries)
        count = max(1, len(countries))
        active = min(count, int(env["WH_COUNTRY_WORKERS"]))
        tabs_per_country = int(env["WH_VERIFY_WORKERS"])
        planned_total = min(5, active * tabs_per_country)
        job.log(f"Concurrency plan: up to {active} country worker(s), "
                f"{planned_total} browser tab(s) total (maximum {tabs_per_country} per country; cap 5).")
        py = sys.executable

        if mode != "estimate" and not os.environ.get("WH_SKIP_PREP"):
            if settings.get("run_tests") and (ROOT / "tests" / "test_pipeline.py").exists():
                # UPDATED LOGIC: tests run with a CLEAN environment (no WH_* settings), otherwise e.g.
                # WH_VERIFY_FRESH_HOURS=720 changes what the freshness tests expect and 5 of them fail.
                test_env = {k: v for k, v in env.items() if not k.startswith("WH_") and k != "GITHUB_RUN_ID"}
                self._exec(job, [py, "-m", "pytest", "tests/test_pipeline.py", "-q", "-p", "no:cacheprovider"],
                           test_env, "run the test suite (report only)")
            if (ROOT / "verify_active.py").exists():
                self._exec(job, [py, "verify_active.py", "--purge-legacy-zeros"], env, "drop unconfirmed zeros from the cache")
        shutil.rmtree(ROOT / "work", ignore_errors=True)
        shutil.rmtree(ROOT / "run_artifact", ignore_errors=True)
        (ROOT / "work").mkdir(exist_ok=True)

        round_minutes = int(params["max_minutes"])
        if params.get("deadline_ts"):
            # what is left of the whole budget now (after the test run), minus ~2 min to write the report
            round_minutes = max(1, int((params["deadline_ts"] - time.time()) / 60) - 2)
            job.log(f"Time budget: {round_minutes} min for this round "
                    f"({params['max_minutes']} min for the whole job).")
        cmd = [py, "-u", str(RUN_SCRIPT), "--countries", ",".join(countries), "--max-calls", str(params["max_calls"]),
               "--from", date_from, "--to", date_to, "--max-minutes", str(round_minutes)]
        if mode == "estimate":
            cmd.append("--estimate")
        elif mode in ("rebuild", "verify"):
            cmd.append("--re-export")
        elif mode == "scan" and params.get("resume") and round_no == 1:
            cmd.append("--resume")
        self._stamps.clear()
        self._country_stamps.clear()
        self._answered = 0
        seeded = {c: {"done": 0, "total": 0, "remaining": 0, "per_minute": None, "note": "starting"}
                  for c in countries}
        job.meta["by_country"] = seeded
        job.meta["live"] = {"country": "", "done": 0, "total": 0, "per_minute": None,
                            "answered": 0, "note": "", "by_country": seeded}
        t0 = time.time()
        rinfo["exit"] = self._exec(job, cmd, env, " ".join(cmd[2:]).replace(settings.get("WH_API_KEY") or "\0", "***"),
                                   track=True)
        minutes = max(1.0, (time.time() - t0) / 60.0)
        if self._answered >= 20:
            rinfo["rate"] = round(self._answered / minutes, 1)        # measured pages per minute of this round
        rinfo["answered"] = self._answered
        job.meta["live"] = None

        # "Save the scan progress": a stopped run never spends a credit twice
        if mode != "estimate":
            (RESULTS / "scan_data").mkdir(parents=True, exist_ok=True)
            for src in (ROOT / "work").glob("scan_*.json"):
                try:
                    with open(src, "rb") as fin, gzip.open(RESULTS / "scan_data" / (src.name + ".gz"), "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                except OSError:
                    pass

        manifest = self._collect(job, rinfo, mode, run_id)
        rinfo["ended"] = now_iso()
        job.event(f"Round {round_no} finished", f"Exit {rinfo['exit']}; report {'saved' if rinfo['report'] else 'not saved'}.",
                  kind="round", round=round_no)
        job.save()
        return {"report": rinfo["report"], "manifest": manifest, "run_id": run_id}

    def _collect(self, job, rinfo, mode, run_id):
        """Copies ONLY this run's files (same rule as the workflow's 'Collect this run's files only')."""
        out = job.dir / "files" / f"round_{rinfo['n']}"
        out.mkdir(parents=True, exist_ok=True)
        manifest, reason = {}, ""
        if mode == "estimate":
            for src in (ROOT / "run_artifact").glob("estimate_report_*"):
                shutil.copy(src, out / src.name)
                rinfo["files"].append(src.name)
                rinfo["report"] = rinfo["report"] or src.suffix == ".html"
            reason = "" if rinfo["report"] else "the estimate produced no report"
        else:
            try:
                manifest = json.loads((RESULTS / "run_manifest.json").read_text(encoding="utf-8"))
            except Exception as exc:
                reason = f"no run manifest was written by this run ({exc})"
            if manifest and str(manifest.get("workflow_run_id") or "") != run_id:
                reason, manifest = "the run manifest belongs to an earlier run: this run produced nothing", {}
            if manifest:
                for key in ("html_path", "workbook_path"):
                    path = Path(manifest.get(key) or "")
                    if manifest.get(key) and not path.is_absolute():
                        path = ROOT / path                    # the manifest stores relative paths
                    if manifest.get(key) and path.is_file():
                        shutil.copy(path, out / path.name)
                        rinfo["files"].append(path.name)
                        rinfo["report"] = True
                rinfo["summary"] = {k: manifest.get(k) for k in (
                    "outcome", "note", "verification", "verified_pages", "pages_in_scope", "verification_percent",
                    "unverified_pages", "stale_pages",
                    "verification_failed", "failed_countries", "partial_countries") if k in manifest}
                if not rinfo["report"]:
                    reason = manifest.get("note") or "this run published no report"
        rinfo["reason"] = reason
        atomic_write(out / "run_status.json", json.dumps({**{k: v for k, v in rinfo.items()}, "reason": reason},
                                                         indent=2, default=str))
        job.log(f"\ncollected: {rinfo['files']}  report produced: {rinfo['report']}  reason: {reason or '-'}")
        return manifest


MANAGER = None


def manager():
    global MANAGER
    if MANAGER is None:
        MANAGER = JobManager()
    return MANAGER
