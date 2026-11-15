"""One-click install of the free Tor program (Windows). Downloads the official Tor Expert Bundle from
dist.torproject.org into <project>/tor/ . Linux / macOS: use the package manager (command is returned)."""
import io
import os
import platform
import re
import tarfile
import urllib.request
from pathlib import Path

from .paths import ROOT

BASE = "https://dist.torproject.org/torbrowser/"
IS_WINDOWS = os.name == "nt"
UA = {"User-Agent": "Mozilla/5.0 (WH-Scanner tor installer)"}


def _get(url, timeout=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as resp:
        return resp.read()


def _safe_extract(data, dest):
    dest = Path(dest).resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest != target and dest not in target.parents:
                raise ValueError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                continue
            tar.extract(member, dest)


def install_tor(log=print):
    """Returns (ok, message)."""
    if not IS_WINDOWS:
        return False, "Linux: sudo apt install tor tor-geoipdb   |   macOS: brew install tor   (then restart the app)"
    arch = "x86_64" if "64" in platform.machine() else "i686"
    try:
        listing = _get(BASE).decode("utf-8", "replace")
    except Exception as exc:
        return False, f"Could not reach dist.torproject.org ({exc}). Check the internet / firewall, or install Tor by hand (GUIDE.md)."
    versions = sorted(set(re.findall(r'href="(\d+(?:\.\d+)+)/"', listing)),
                      key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    dest = ROOT / "tor"
    for version in versions[:6]:
        name = f"tor-expert-bundle-windows-{arch}-{version}.tar.gz"
        log(f"Downloading {name} ...")
        try:
            data = _get(f"{BASE}{version}/{name}", timeout=300)
        except Exception as exc:
            log(f"  not available ({exc})")
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            _safe_extract(data, dest)
        except Exception as exc:
            return False, f"Downloaded but could not unpack: {exc}"
        exe = dest / "tor" / "tor.exe"
        if exe.is_file():
            return True, f"Tor {version} installed in {dest}"
    return False, "No Tor Expert Bundle could be downloaded. Install it by hand (GUIDE.md section 4)."