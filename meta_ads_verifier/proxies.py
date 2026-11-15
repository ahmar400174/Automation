"""Per-request residential proxy rotation.

Every call returns a new exit. Session ids are rewritten into the provider
username or password so a rotating gateway cannot stick to the previous IP.
Failed exits are dropped by simply never reusing that session id. There is
no sleep, no shared pause, and no empty-pool wait.
"""

from __future__ import annotations

import itertools
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

SESSION_MODES = (
    "auto",
    "none",
    "brightdata",
    "oxylabs",
    "smartproxy",
    "iproyal",
    "soax",
)


def _detect_mode(hostname: str) -> str:
    host = (hostname or "").lower()
    if "superproxy" in host or "brightdata" in host or "luminati" in host:
        return "brightdata"
    if "oxylabs" in host:
        return "oxylabs"
    if "smartproxy" in host or "decodo" in host:
        return "smartproxy"
    if "iproyal" in host or "evomi" in host:
        return "iproyal"
    if "soax.com" in host:
        return "soax"
    return "none"


def _rebuild(original: str, username: str | None, password: str | None) -> str:
    parts = urlsplit(original)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password is not None:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return urlunsplit((parts.scheme, auth + host + port, parts.path, parts.query, parts.fragment))


def inject_session(proxy_url: str, mode: str, session_id: str | None = None) -> str:
    """Return proxy_url pinned to a brand-new provider session id."""
    if not proxy_url:
        return proxy_url
    sid = session_id or secrets.token_hex(8)
    if "{session}" in proxy_url:
        return proxy_url.replace("{session}", sid)
    parts = urlsplit(proxy_url)
    username = parts.username or ""
    password = parts.password or ""
    resolved = mode
    if resolved == "auto":
        resolved = _detect_mode(parts.hostname or "")
    if resolved == "brightdata":
        username = re_sub_session(username)
        username = f"{username}-session-{sid}"
    elif resolved == "oxylabs":
        username = re_sub_token(username, "sessid")
        username = re_sub_token(username, "sesstime")
        username = f"{username}-sessid-{sid}-sesstime-1"
    elif resolved == "smartproxy":
        username = re_sub_session(username)
        username = f"{username}-session-{sid}"
    elif resolved == "iproyal":
        password = re_sub_password_session(password)
        password = f"{password}_session-{sid}_lifetime-1m"
    elif resolved == "soax":
        username = re_sub_token(username, "sessionid")
        username = re_sub_token(username, "sessionlength")
        username = f"{username}-sessionid-{sid}-sessionlength-60"
    else:
        return proxy_url
    return _rebuild(proxy_url, username, password)


def re_sub_session(username: str) -> str:
    return re_sub_token(username, "session")


def re_sub_token(username: str, token: str) -> str:
    import re

    return re.sub(rf"-{token}-[A-Za-z0-9]+", "", username)


def re_sub_password_session(password: str) -> str:
    import re

    password = re.sub(r"_session-[A-Za-z0-9]+", "", password)
    password = re.sub(r"_lifetime-[A-Za-z0-9]+", "", password)
    return password


class ProxyRotator:
    """Round-robin of gateway URLs. Each `next()` is a brand-new exit."""

    def __init__(self, proxies: list[str] | None, session_mode: str = "auto"):
        cleaned = []
        for proxy in proxies or []:
            value = (proxy or "").strip()
            if value and not value.startswith("#"):
                cleaned.append(value)
        self.proxies = cleaned
        self.session_mode = session_mode
        self._cycle = itertools.cycle(cleaned) if cleaned else None
        self.dropped = 0

    def next(self) -> str | None:
        if self._cycle is None:
            return None
        base = next(self._cycle)
        return inject_session(base, self.session_mode)

    def drop(self, proxy: str | None) -> None:
        """Forget a burned exit. The next `next()` is already a new session.

        This does not sleep and does not stop other workers. `proxy` is unused
        on purpose: quarantining a gateway would stall the queue when every
        worker shares one rotating endpoint.
        """
        self.dropped += 1


def load_proxy_list(proxy: str | None, proxy_file: str | None) -> list[str]:
    found: list[str] = []
    if proxy:
        found.append(proxy.strip())
    if proxy_file:
        from pathlib import Path

        for line in Path(proxy_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(line)
    return found
