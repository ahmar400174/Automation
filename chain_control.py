"""
Decides whether the workflow must start ANOTHER verification round by itself.

When a run ends with pages Meta has not answered, the app may continue with another verify-only round on the
same host and route. It retries only unanswered work; it does not change identity or claim that a blocked source
will eventually respond. In the app, time, round, and no-progress ceilings are disabled by default; a user stop,
completion, or unrecoverable run failure ends the job.

Optional limits can be configured by standalone callers through WH_CHAIN_MAX_HOURS,
WH_CHAIN_MAX_ROUNDS, and WH_CHAIN_MAX_STALLS. The web runner explicitly sets them to zero.
Cancelled runs, missing manifests, failed reports, or completed reports never continue.

State lives in results/chain_state.json (committed with the other results).

    python chain_control.py begin    # first step of a run: new chain, or continuation of one?
    python chain_control.py end      # last decision step: writes GitHub outputs continue/from/to/countries
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_FILE = Path(os.environ.get("WH_CHAIN_STATE", "results/chain_state.json"))
MANIFEST_FILE = Path(os.environ.get("WH_CHAIN_MANIFEST", "results/run_manifest.json"))
# A run that starts within this many seconds after the previous one ended continues the chain.
CONTINUE_WINDOW = 45 * 60


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fresh_state(now):
    return {"started": now, "depth": 0, "last": None, "stalls": 0}


def begin_state(state, now):
    """State at the start of a run: a continuation of the running chain, or the start of a new one."""
    ended = state.get("ended")
    if ended is not None and state.get("started") is not None and 0 <= now - ended <= CONTINUE_WINDOW:
        return dict(state)
    return fresh_state(now)


def decide(state, manifest, run_id, now, max_hours=0.0, max_rounds=0, max_stalls=0):
    """Returns (continue?, reason, new_state)."""
    state = dict(state) if state.get("started") is not None else fresh_state(now)
    if not manifest or str(manifest.get("workflow_run_id")) != str(run_id):
        return False, "this run produced no report: not continuing", fresh_state(now)
    # Explicit failed outcome (no workbook produced) must never look like "everything verified".
    # Test / older manifests may omit html_path; only the explicit failed flag is authoritative.
    if str(manifest.get("outcome") or "").lower() == "failed":
        return False, "this run produced no report: not continuing", fresh_state(now)
    unclear = (_int(manifest.get("unverified_pages")) + _int(manifest.get("stale_pages"))
               + _int(manifest.get("verification_failed")))
    if unclear == 0:
        return False, "every page is verified: done", fresh_state(now)
    if max_hours > 0 and now - state["started"] > max_hours * 3600:
        return False, f"time limit of {max_hours:g} h reached with {unclear} page(s) still unverified", \
            fresh_state(now)
    if max_rounds > 0 and state.get("depth", 0) >= max_rounds:
        return False, f"{max_rounds} automatic rounds used, {unclear} page(s) still unverified", fresh_state(now)
    last = state.get("last")
    stalls = state.get("stalls", 0) + 1 if (last is not None and unclear >= last) else 0
    if max_stalls > 0 and stalls >= max_stalls:
        return False, f"no progress in {max_stalls} rounds in a row, {unclear} page(s) still unverified", fresh_state(now)
    state.update(depth=state.get("depth", 0) + 1, last=unclear, stalls=stalls, ended=now)
    return True, f"{unclear} page(s) still unverified: starting automatic round {state['depth']}", state


def _output(name, value):
    target = os.environ.get("GITHUB_OUTPUT")
    line = f"{name}={str(value).replace(chr(10), ' ')}\n"
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
    print(line.strip())


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    command = argv[0] if argv else "end"
    now = time.time()
    if command == "begin":
        state = begin_state(_read(STATE_FILE), now)
        _write(STATE_FILE, state)
        limit = int(os.environ.get("WH_CHAIN_MAX_ROUNDS", "0") or 0)
        print(f"chain: round {state.get('depth', 0)}" + (f" of at most {limit}" if limit else " (no round limit)"))
        return 0
    manifest = _read(MANIFEST_FILE)
    go, reason, state = decide(
        _read(STATE_FILE), manifest, os.environ.get("GITHUB_RUN_ID", ""), now,
        max_hours=float(os.environ.get("WH_CHAIN_MAX_HOURS", "0") or 0),
        max_rounds=int(os.environ.get("WH_CHAIN_MAX_ROUNDS", "0") or 0),
        max_stalls=int(os.environ.get("WH_CHAIN_MAX_STALLS", "0") or 0))
    _write(STATE_FILE, state)
    print(f"chain decision: {reason}")
    _output("continue", "true" if go else "false")
    _output("reason", reason)
    if go:
        _output("date_from", manifest.get("from", ""))
        _output("date_to", manifest.get("to", ""))
        # UPDATED LOGIC: never char-split; repair string / letter-split values before passing to next round
        _c = manifest.get("countries") or []
        _t = [x for x in re.split(r"[\s,;|]+", (",".join(_c) if isinstance(_c, list) else str(_c)).upper()) if x]
        if _t and all(len(x) == 1 for x in _t):
            _j = "".join(_t)
            _t = [_j[i:i + 2] for i in range(0, len(_j), 2)]
        _output("countries", ",".join(dict.fromkeys(x for x in _t if len(x) == 2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
