"""Folders. APP_DIR = where this code lives. ROOT = the working folder holding run_weekly.py, results/, work/.
(They are the same folder normally; WH_ROOT only exists so the automatic tests can use a throw-away folder.)"""
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("WH_ROOT") or APP_DIR).resolve()
DATA = Path(os.environ.get("WH_DATA_DIR") or (ROOT / "data")).resolve()
RUNS = DATA / "runs"
RESULTS = ROOT / "results"
for _p in (DATA, RUNS, RESULTS):
    _p.mkdir(parents=True, exist_ok=True)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def atomic_write(path, text, tries=40):
    """Writes a file safely. On Windows os.replace() fails with PermissionError while another thread / the
    browser poll / an antivirus has the file open for a moment, so it retries and finally falls back to a
    direct write. It never raises (a failed status save must not kill a running job)."""
    import time
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(tries):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1) if attempt < 10 else 0.25)
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False