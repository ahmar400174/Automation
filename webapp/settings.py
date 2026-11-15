"""Settings page storage (data/settings.json) + tiny .env reader. Secrets never leave the server."""
import json
import os
import threading
from pathlib import Path

from .paths import DATA, ROOT, atomic_write

FILE = DATA / "settings.json"
_LOCK = threading.Lock()

DEFAULTS = {
    "settings_version": 2,       # one-time migration to direct host networking by default
    "WH_API_KEY": "",            # WinningHunter API key (needed for estimate / scan)
    "META_API_TOKEN": "",        # ignored. This build does not call the Meta API.
    "tor_circuits": 1,           # one steady route; exit rotation is disabled
    "fresh_hours": 72,           # a Meta answer older than this is asked again
    "WH_PROXY": "",              # legacy value; ignored (no paid/rotating proxy in this build)
    "use_tor": False,            # direct host route is the default; Tor remains opt-in
    "tor_failover": True,        # direct = fast; Tor only while Meta rate-limits, then back to direct
    "tor_path": "",              # empty = find it automatically
    "run_tests": False,          # optional pre-run test suite; keep live verification ready by default
    "chain_max_hours": 0,        # zero = no time ceiling
    "chain_max_rounds": 0,       # zero = no round ceiling
    "chain_max_stalls": 0,       # zero = a temporary source block does not abandon the run
    "chain_pause_seconds": "60", # routine pause between automatic rounds; source rate limits take precedence
    "tor_rotate_every": 0,       # legacy setting; never used
    "verify_workers": 4,         # up to four tabs per country, aggregate cap is five
    "verify_gap": 0.75,          # jittered request gap is bounded between 0.5 and 1.5 seconds
    "country_workers": 5,        # at most five concurrent country queues under the global five-tab cap
    "web_password": "",          # empty = no login (only do that when the app listens on 127.0.0.1)
}
SECRET_KEYS = ("WH_API_KEY", "WH_PROXY", "META_API_TOKEN", "web_password")


def load_dotenv():
    """Reads ROOT/.env (KEY=VALUE lines) into os.environ without overriding real environment variables."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load():
    data = dict(DEFAULTS)
    with _LOCK:
        try:
            stored = json.loads(FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                try:
                    version = int(stored.get("settings_version") or 0)
                except (TypeError, ValueError):
                    version = 0
                if version < 2:
                    # The previous default enabled Tor. Migrate existing installs once; users can
                    # explicitly re-enable it in Settings after this persisted version marker.
                    stored["use_tor"] = False
                    stored["settings_version"] = 2
                    try:
                        atomic_write(FILE, json.dumps(stored, indent=2))
                        try:
                            os.chmod(FILE, 0o600)
                        except OSError:
                            pass
                    except OSError:
                        pass
                data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
    for key in ("WH_API_KEY", "WH_PROXY", "META_API_TOKEN"):           # environment / .env is the fallback
        if not data.get(key) and os.environ.get(key):
            data[key] = os.environ[key]
    if not data.get("web_password") and os.environ.get("WH_WEB_PASSWORD"):
        data["web_password"] = os.environ["WH_WEB_PASSWORD"]
    return data


def save(updates):
    current = load()
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        if key in SECRET_KEYS and value == "__keep__":
            continue
        if isinstance(DEFAULTS[key], bool):
            value = bool(value)
        elif isinstance(DEFAULTS[key], int) and not isinstance(DEFAULTS[key], bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = DEFAULTS[key]
        else:
            value = str(value or "").strip()
        current[key] = value
    with _LOCK:
        atomic_write(FILE, json.dumps(current, indent=2))      # UPDATED LOGIC: Windows-safe
        try:
            os.chmod(FILE, 0o600)
        except OSError:
            pass
    return current


def masked(value):
    value = str(value or "")
    if not value:
        return ""
    return value[:3] + "•" * 6 + value[-2:] if len(value) > 8 else "•" * len(value)
