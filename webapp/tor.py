"""Start one optional Tor route for public Ad Library reads.

The route is shared across countries and is never intentionally rotated. Tor does not guarantee
access, speed, or a particular exit country; if it cannot bootstrap, the app reports and uses
the ordinary network route.
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .paths import DATA, ROOT

TOR_DIR = DATA / "tor"


def find_tor(custom=""):
    candidates = []
    if custom:
        candidates.append(Path(custom))
    found = shutil.which("tor") or shutil.which("tor.exe")
    if found:
        candidates.append(Path(found))
    candidates += [
        ROOT / "tor" / "tor" / "tor.exe", ROOT / "tor" / "tor.exe", ROOT / "tor" / "tor",
        Path("C:/Tor/tor/tor.exe"), Path("C:/Tor/tor.exe"),
        Path.home() / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path("/usr/bin/tor"), Path("/usr/local/bin/tor"), Path("/opt/homebrew/bin/tor"),
    ]
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return ""


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _geoip_lines(tor_exe):
    base = Path(tor_exe).resolve().parent
    for folder in (base / "data", base.parent / "data", base):
        geoip, geoip6 = folder / "geoip", folder / "geoip6"
        if geoip.is_file():
            lines = [f"GeoIPFile {geoip.as_posix()}"]
            if geoip6.is_file():
                lines.append(f"GeoIPv6File {geoip6.as_posix()}")
            return lines
    return []


def circuits_for(country_count, requested=None):
    """Return one steady circuit; requested parallel circuits are intentionally ignored."""
    return 1


class TorFarm:
    def __init__(self, tor_exe, log=print, bridges=None, circuits=None, **kwargs):
        self.tor_exe = tor_exe
        self.log = log
        self.procs = {}          # ("shared", 0) -> Popen
        self.ports = {}          # country -> the same single steady SOCKS route
        self.logs = {}
        self.circuits = circuits
        self.bridges = bridges
        if kwargs:
            log(f"TorFarm: ignored extra options {', '.join(sorted(kwargs))}")
        if bridges:
            log("Tor bridge settings are ignored by this simple route configuration.")

    def _launch_one(self, cc, index, stamp, geo, prefer_exit=False):
        """Start one Tor process without pinning or rotating its exit identity."""
        try:
            socks = _free_port()
            ctrl = 0
            folder = TOR_DIR / f"data_{cc}_{index}"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(folder, 0o700)
            except OSError:
                pass
            logfile = TOR_DIR / f"tor_{cc}_{index}_{stamp}.log"
            self.logs[(cc, index)] = logfile
            rc = TOR_DIR / f"torrc_{cc}_{index}"
            lines = [
                f"SocksPort 127.0.0.1:{socks}",
                f"DataDirectory {folder.as_posix()}",
                "CircuitBuildTimeout 30",
                "LearnCircuitBuildTimeout 0",
                "ClientOnly 1",
                f"Log notice file {logfile.as_posix()}",
                *geo,
            ]
            rc.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if os.name == "nt":
                proc = subprocess.Popen([str(self.tor_exe), "-f", str(rc)], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=0x08000000)
            else:
                proc = subprocess.Popen([str(self.tor_exe), "-f", str(rc)], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
            self.procs[(cc, index)] = proc
            return socks, ctrl
        except OSError as exc:
            self.log(f"Tor {cc}#{index}: could not start ({exc}).")
            return None, None

    def _wait_ready(self, cc, index, socks, ctrl, deadline):
        logfile = self.logs.get((cc, index))
        while time.time() < deadline:
            try:
                if logfile and "Bootstrapped 100%" in logfile.read_text(
                        encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
            proc = self.procs.get((cc, index))
            if proc is None or proc.poll() is not None:
                return False
            time.sleep(1.2)
        return False

    def start(self, countries, wait=240):
        """Start one unpinned, non-rotating Tor route and share it across requested countries."""
        TOR_DIR.mkdir(parents=True, exist_ok=True)
        geo = _geoip_lines(self.tor_exe)
        stamp = str(int(time.time()))
        self.ports = {str(cc).upper(): [] for cc in countries}
        self.log("Tor route: one shared, unpinned circuit; no NEWNYM or per-page IP changes.")
        socks, ctrl = self._launch_one("shared", 0, stamp, geo, prefer_exit=False)
        available = socks is not None and self._wait_ready("shared", 0, socks, ctrl, time.time() + wait)
        if available:
            self.log(f"Tor route ready on local SOCKS port {socks}.")
            self.ports = {str(cc).upper(): [(socks, ctrl)] for cc in countries}
        else:
            self.log("Tor did not bootstrap; continuing without Tor.")
            self._kill("shared", 0)
        return {str(cc).upper(): bool(available) for cc in countries}

    def env_value(self):
        parts = []
        for cc, pairs in self.ports.items():
            for socks, ctrl in pairs:
                parts.append(f"{cc}:{socks}:{ctrl}")
        return ",".join(parts)

    def _kill(self, cc, index):
        proc = self.procs.pop((cc, index), None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stop(self):
        for key in list(self.procs):
            self._kill(*key)
        self.ports.clear()
