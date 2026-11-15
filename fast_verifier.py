"""
fast_verifier.py - high-throughput async Meta Ad Library verifier.

DATA SAFETY: every verified page is appended to <out>.jsonl the instant it finishes.
Ctrl+C / crash / power cut => nothing verified is lost. Re-run the SAME command and it
resumes (skips pages already verified OK, retries only the failed/unfinished ones).
ROUTE FAILOVER (--tor): runs DIRECT (fast). When Meta rate-limits, it switches to Tor *without pausing*;
a small probe keeps testing the direct route and the moment it works again, it goes back to DIRECT.
Needs Tor running: Tor Browser = 127.0.0.1:9150, tor service = 127.0.0.1:9050.
xlsx/csv/html snapshots are rebuilt from that journal every 50 records or 15 s, and once more on exit.

pip install curl_cffi tqdm pandas openpyxl
Usage:
  python fast_verifier.py --input pages.xlsx --id-col page_id --proxies proxies.txt \
      --workers 100 --out results/verified

proxies.txt: one per line, e.g. http://user:pass@gw.provider.com:8000
  * Rotating-gateway proxy (one host, new IP per request): put that single line; every
    request opens a fresh connection so the provider hands out a new exit IP.
  * List of static proxies: put many lines; each request picks a random live one and a
    proxy that returns 403/429/timeout is dropped from the pool immediately.
"""
from __future__ import annotations
import argparse, asyncio, json, os, random, re, time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd
from curl_cffi.requests import AsyncSession
from tqdm.asyncio import tqdm

IMPERSONATE = "chrome120"
URL = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
       "&country=ALL&view_all_page_id={page_id}")
MAX_ATTEMPTS = 8          # per page, each with a FRESH proxy; no sleeping between attempts
REQ_TIMEOUT = 20
FLUSH_EVERY = 50
PAGE_HARD_TIMEOUT = 150   # absolute max seconds per page (all retries included)
BLOCK_CODES = {403, 429, 407, 502, 503}
BLOCK_TRIGGER = 3         # this many blocked responses in a row on DIRECT => switch to Tor
PROBE_EVERY = 15          # seconds between direct-route probes while on Tor
PROBE_NEEDED = 2          # consecutive good probes before returning to DIRECT
LIMIT_MARKERS = ("rate limit", "temporarily blocked", "try again later", "you must log in")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}


# --------------------------------------------------------------------------- parsing
def parse_page(text: str) -> dict:
    """ADAPT THIS to your existing parser. Meta renders much of the Ad Library client-side,
    so returned HTML may carry the count only inside embedded JSON blobs."""
    m = (re.search(r'"(?:total_count|totalCount)"\s*:\s*(\d+)', text)
         or re.search(r"~?\s*([\d,]+)\s+results?", text))
    count = int(m.group(1).replace(",", "")) if m else None
    ids = list(dict.fromkeys(re.findall(r'"ad_archive_id"\s*:\s*"?(\d{10,})', text)))[:200]
    return {"active_ads": count if count is not None else len(ids), "library_ids": ",".join(ids)}


# --------------------------------------------------------------------------- proxy pool
class ProxyPool:
    def __init__(self, proxies: list[str]):
        self.all = proxies[:]
        self.live = set(proxies)
        self.rotating = len(proxies) == 1          # single gateway = provider rotates IPs
        self.dropped = 0

    def get(self) -> str | None:
        if not self.all:
            return None
        if self.rotating:
            return self.all[0]
        if not self.live:                           # never starve: revive everything instantly
            self.live = set(self.all)
        return random.choice(tuple(self.live))

    def drop(self, p: str | None):
        if p and not self.rotating and p in self.live:
            self.live.discard(p)
            self.dropped += 1


# --------------------------------------------------------------------------- stats
@dataclass
class Stats:
    started: float = field(default_factory=time.perf_counter)
    processed: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    active: int = 0

    @property
    def rate(self): return self.processed / max(time.perf_counter() - self.started, 1e-6)
    @property
    def success(self): return 100 * self.ok / max(self.processed, 1)


# --------------------------------------------------------------------------- route failover
def looks_limited(text: str) -> bool:
    t = text[:20000].lower()
    return any(m in t for m in LIMIT_MARKERS) and "ad_archive_id" not in t


class RouteManager:
    """DIRECT <-> TOR switch. Never pauses the queue: workers just use whichever route is current."""
    def __init__(self, enabled: bool, tor_proxy: str, ctrl_port: int, ctrl_pw: str, tor_conc: int):
        self.enabled, self.tor_proxy = enabled, tor_proxy
        self.ctrl_port, self.ctrl_pw = ctrl_port, ctrl_pw
        self.mode = "DIRECT"
        self.consec = 0
        self.probe_ok = 0
        self.switches = 0
        self.tor_sem = asyncio.Semaphore(tor_conc)     # Tor is slow: cap what we push through it
        self._last_newnym = 0.0

    def _switch(self, mode: str, why: str):
        if self.mode != mode:
            self.mode = mode; self.switches += 1; self.consec = 0; self.probe_ok = 0
            print(f"\n[route] -> {mode} ({why})")

    def report(self, mode: str, blocked: bool):
        if not self.enabled or mode != "DIRECT":
            return
        if blocked:
            self.consec += 1
            if self.consec >= BLOCK_TRIGGER:
                self._switch("TOR", f"{self.consec} blocks in a row on direct")
        else:
            self.consec = 0

    async def newnym(self):
        """Ask Tor for a new exit IP (Tor allows ~1 per 10 s). Silent if control port isn't available."""
        if time.perf_counter() - self._last_newnym < 10:
            return
        self._last_newnym = time.perf_counter()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.ctrl_port), 3)
            w.write(f'AUTHENTICATE "{self.ctrl_pw}"\r\nSIGNAL NEWNYM\r\nQUIT\r\n'.encode())
            await w.drain(); await asyncio.wait_for(r.read(200), 3); w.close()
        except Exception:
            pass

    async def probe_loop(self, pool, probe_id: str):
        """While on Tor, keep testing the DIRECT route with one light request."""
        while True:
            await asyncio.sleep(PROBE_EVERY)
            if not self.enabled or self.mode != "TOR":
                continue
            proxy = pool.get()
            try:
                async with AsyncSession(impersonate=IMPERSONATE, timeout=REQ_TIMEOUT,
                                        proxies={"http": proxy, "https": proxy} if proxy else None) as s:
                    r = await s.get(URL.format(page_id=probe_id), headers=HEADERS)
                good = r.status_code == 200 and not looks_limited(r.text)
            except Exception:
                good = False
            self.probe_ok = self.probe_ok + 1 if good else 0
            if self.probe_ok >= PROBE_NEEDED:
                self._switch("DIRECT", "Meta limit cleared")


# --------------------------------------------------------------------------- worker
async def verify_one(page_id: str, pool: ProxyPool, st: Stats, sem: asyncio.Semaphore,
                     route: RouteManager) -> dict:
    async with sem:
        st.active += 1
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                mode = route.mode if route.enabled else "DIRECT"
                proxy = route.tor_proxy if mode == "TOR" else pool.get()
                if mode == "TOR":
                    await route.tor_sem.acquire()
                try:
                    # New session per request = new TLS handshake + new connection => new exit IP
                    async with AsyncSession(impersonate=IMPERSONATE, timeout=REQ_TIMEOUT,
                                            proxies={"http": proxy, "https": proxy} if proxy else None) as s:
                        r = await s.get(URL.format(page_id=page_id), headers=HEADERS, allow_redirects=True)
                    blocked = (r.status_code in BLOCK_CODES or "/login" in str(r.url)
                               or (r.status_code == 200 and looks_limited(r.text)))
                    route.report(mode, blocked)
                    if blocked:
                        st.retries += 1
                        if mode == "TOR":
                            asyncio.create_task(route.newnym())   # new exit IP, retry instantly
                        else:
                            pool.drop(proxy)
                        continue                                  # instant retry, NO sleep, NO global pause
                    if r.status_code == 200:
                        st.ok += 1
                        return {"page_id": page_id, "status": "ok", "attempts": attempt,
                                "route": mode, **parse_page(r.text)}
                    st.retries += 1
                except Exception:                                 # timeout / proxy error / TLS reset
                    route.report(mode, True)
                    if mode == "DIRECT":
                        pool.drop(proxy)
                    st.retries += 1
                finally:
                    if mode == "TOR":
                        route.tor_sem.release()
            st.failed += 1
            return {"page_id": page_id, "status": "failed", "attempts": MAX_ATTEMPTS,
                    "route": route.mode, "active_ads": None, "library_ids": ""}
        finally:
            st.active -= 1
            st.processed += 1


# --------------------------------------------------------------------------- durable results store
def load_journal(path: Path) -> dict[str, dict]:
    res: dict[str, dict] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    res[str(r["page_id"])] = r                    # later line wins
                except Exception:
                    continue                                      # half-written last line after hard kill
    return res


def write_outputs(journal_path: str, base: str) -> str:
    """Rebuilds csv / xlsx / html from the journal. Runs in a SEPARATE PROCESS while scanning
    (so openpyxl can never hold the GIL and freeze the network workers). Never raises."""
    jp, base_p = Path(journal_path), Path(base)
    rows = list(load_journal(jp).values())
    if not rows:
        return "empty"
    df = pd.DataFrame(rows)
    msgs = []
    try:
        df.to_csv(base_p.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    except Exception as e:
        msgs.append(f"csv failed: {e}")
    try:
        tmp = base_p.with_name(base_p.name + ".tmp.xlsx")
        df.to_excel(tmp, index=False)
        tmp.replace(base_p.with_suffix(".xlsx"))
    except PermissionError:                                       # xlsx is open in Excel
        alt = base_p.with_name(f"{base_p.name}_{int(time.time())}.xlsx")
        try:
            df.to_excel(alt, index=False); msgs.append(f"xlsx open in Excel -> saved {alt.name}")
        except Exception as e:
            msgs.append(f"xlsx failed: {e}")
    except Exception as e:
        msgs.append(f"xlsx failed: {e}")
    try:
        from report_builder import build_html_report
        if {"library_id", "launched"}.issubset(df.columns):
            build_html_report(df, str(base_p.with_suffix(".html")))
    except ImportError:
        pass
    except Exception as e:
        msgs.append(f"html failed: {e}")
    return "; ".join(msgs) or "ok"


def _child_init():
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)                  # Ctrl+C must not kill a save halfway


class Store:
    """Journal (append-only, flushed per record). Snapshots are produced by a background PROCESS."""
    def __init__(self, base: Path):
        self.base = base
        self.journal_path = base.with_suffix(".jsonl")
        self.results: dict[str, dict] = {}
        self.pending = 0
        self.last_snap = time.perf_counter()
        self.fh = None

    def load(self) -> int:
        self.results = load_journal(self.journal_path)
        return len(self.results)

    def done_ok(self) -> set[str]:
        return {k for k, v in self.results.items() if v.get("status") == "ok"}

    def open(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.journal_path, "a", encoding="utf-8")

    def add(self, r: dict):
        self.results[str(r["page_id"])] = r
        if self.fh:
            self.fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.fh.flush()                                       # in OS cache => survives Ctrl+C / kill
        self.pending += 1

    def sync_journal(self):
        try:
            if self.fh:
                self.fh.flush(); os.fsync(self.fh.fileno())      # survives power loss too
        except Exception:
            pass

    def final_snapshot(self):
        """Used once at exit (blocking is fine then)."""
        self.sync_journal()
        msg = write_outputs(str(self.journal_path), str(self.base))
        if msg not in ("ok", "empty"):
            print(f"\n[warn] {msg}")

    def close(self):
        try:
            if self.fh:
                self.fh.close()
        except Exception:
            pass


async def snapshot_loop(store: Store):
    """Every 50 new records OR every 15 s -> save in a separate process; scanning NEVER waits for it.
    If the previous save is still running, the next one is simply skipped (coalesced)."""
    from concurrent.futures import ProcessPoolExecutor
    loop = asyncio.get_running_loop()
    ex = ProcessPoolExecutor(max_workers=1, initializer=_child_init)
    fut = None
    try:
        while True:
            await asyncio.sleep(1)
            if fut is not None and fut.done():
                try:
                    msg = fut.result()
                    if msg not in ("ok", "empty"):
                        print(f"\n[warn] {msg}")
                except Exception as e:
                    print(f"\n[warn] background save error: {e}")
                fut = None
            due = store.pending >= FLUSH_EVERY or (store.pending > 0 and time.perf_counter() - store.last_snap >= 15)
            if due and fut is None:
                store.sync_journal()
                store.pending = 0; store.last_snap = time.perf_counter()
                fut = loop.run_in_executor(ex, write_outputs, str(store.journal_path), str(store.base))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- main
async def run(ids: list[str], proxies: list[str], workers: int, store: Store, already: int, route_cfg: dict):
    pool, st = ProxyPool(proxies), Stats()
    route = RouteManager(**route_cfg)
    sem = asyncio.Semaphore(workers)
    bar = tqdm(total=len(ids), unit="pg", dynamic_ncols=True)
    if already:
        print(f"Resuming: {already} pages already verified OK, {len(ids)} left to do.")

    async def monitor():
        while True:
            bar.set_postfix_str(f"{st.rate:.1f} pg/s | done {st.processed} | ok {st.success:.1f}% "
                                f"| conns {st.active} | retries {st.retries} | saved {len(store.results)} | route {route.mode}",
                                refresh=False)
            bar.n = st.processed; bar.refresh()
            await asyncio.sleep(0.5)

    async def job(pid):
        try:   # hard ceiling per page so nothing can ever hang the run
            r = await asyncio.wait_for(verify_one(pid, pool, st, sem, route), timeout=PAGE_HARD_TIMEOUT)
        except asyncio.TimeoutError:
            st.failed += 1; st.processed += 1
            r = {"page_id": pid, "status": "failed", "attempts": 0, "active_ads": None, "library_ids": ""}
        store.add(r)                                              # written to disk immediately

    tasks = [asyncio.create_task(monitor()), asyncio.create_task(snapshot_loop(store))]
    if route.enabled and ids:
        tasks.append(asyncio.create_task(route.probe_loop(pool, ids[0])))
    try:
        await asyncio.gather(*(job(p) for p in ids))
    finally:
        for t in tasks:
            t.cancel()
        bar.n = st.processed; bar.refresh(); bar.close()
        print(f"Stopped: {st.ok} ok / {st.failed} failed this session "
              f"({st.rate*60:.0f} pages/min). Route switches: {route.switches}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--id-col", default="page_id")
    ap.add_argument("--proxies", default=None); ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--out", default="results/verified")
    ap.add_argument("--tor", action="store_true", help="fail over to Tor while Meta rate-limits, return to direct when cleared")
    ap.add_argument("--tor-proxy", default="socks5h://127.0.0.1:9050", help="Tor Browser uses 9150")
    ap.add_argument("--tor-control-port", type=int, default=9051)
    ap.add_argument("--tor-control-password", default="")
    ap.add_argument("--tor-concurrency", type=int, default=15)
    ap.add_argument("--fresh", action="store_true", help="ignore old journal, start from zero")
    a = ap.parse_args()
    df = pd.read_csv(a.input) if a.input.endswith(".csv") else pd.read_excel(a.input)
    ids = df[a.id_col].dropna().astype(str).str.strip().drop_duplicates().tolist()
    proxies = [l.strip() for l in Path(a.proxies).read_text().splitlines() if l.strip()] if a.proxies else []
    if not proxies:
        print("WARNING: no proxies - you will be blocked almost immediately at this rate.")

    store = Store(Path(a.out))
    if a.fresh and store.journal_path.exists():
        store.journal_path.rename(store.journal_path.with_suffix(f".old{int(time.time())}.jsonl"))
    store.load()
    done = store.done_ok()
    todo = [i for i in ids if i not in done]
    store.open()
    try:
        cfg = dict(enabled=a.tor, tor_proxy=a.tor_proxy, ctrl_port=a.tor_control_port,
                   ctrl_pw=a.tor_control_password, tor_conc=a.tor_concurrency)
        asyncio.run(run(todo, proxies, a.workers, store, len(done & set(ids)), cfg))
    except KeyboardInterrupt:
        print("\nCtrl+C received - saving everything verified so far...")
    finally:
        store.final_snapshot()                                    # final save, always runs
        store.close()
        print(f"Saved {len(store.results)} verified records -> {store.base}.xlsx / .csv / .jsonl")
        print("Re-run the same command to continue from where it stopped.")


if __name__ == "__main__":
    main()
