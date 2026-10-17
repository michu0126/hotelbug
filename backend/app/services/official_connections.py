"""Private, project-owned Cookie connections shared by API and browser Worker.

Cookie values live only in the existing private browser-data volume, not SQL,
job payloads, API responses or logs. File permissions are enforced on Linux.
"""

import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

DOMAINS = {
    "marriott": ("marriott.com", "marriott.com.cn"),
    "ihg": ("ihg.com",),
    "hilton": ("hilton.com",),
    "hyatt": ("hyatt.com",),
    "accor": ("accor.com",),
    "gha": ("ghadiscovery.com",),
}
ORIGINS = {name: "https://www." + domains[0] for name, domains in DOMAINS.items()}
ORIGINS["accor"] = "https://all.accor.com"
NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LIMIT = 262144
_SAVE_LOCKS = {provider: threading.Lock() for provider in DOMAINS}


def allowed_domain(provider, domain):
    host = domain.removeprefix(".").lower()
    return (
        bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", host))
        and not host.endswith(".")
        and any(host == root or host.endswith("." + root) for root in DOMAINS.get(provider, ()))
    )


def official_origin(provider, origin):
    if provider not in DOMAINS or not isinstance(origin, str):
        raise ValueError("Select a supported hotel group and official HTTPS origin")
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or not allowed_domain(provider, url.hostname)
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("Use this hotel group's official HTTPS origin, without path or credentials")
    return "https://" + url.hostname.lower()


def parse_cookies(provider, origin, raw, now=None):
    origin = official_origin(provider, origin)
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > LIMIT:
        raise ValueError("Cookie input is empty or too large")
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    raw = raw.strip()
    if raw.startswith(("[", "{")):
        try:
            rows = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError("Invalid Cookie JSON") from None
        if isinstance(rows, dict):
            rows = rows.get("cookies")
        if not isinstance(rows, list):
            raise ValueError("Use a Cookie JSON array or an object containing cookies")
    elif "\t" in raw:
        rows = []
        for line in raw.splitlines():
            if line.startswith("#HttpOnly_"):
                line, http_only = line[len("#HttpOnly_") :], True
            elif line.startswith("#") or not line.strip():
                continue
            else:
                http_only = False
            fields = line.split("\t")
            if len(fields) != 7 or fields[1] not in ("TRUE", "FALSE") or fields[3] not in ("TRUE", "FALSE"):
                raise ValueError("Invalid Netscape Cookie file")
            rows.append(
                dict(
                    domain=fields[0],
                    path=fields[2],
                    secure=fields[3] == "TRUE",
                    expires=fields[4],
                    name=fields[5],
                    value=fields[6],
                    httpOnly=http_only,
                    hostOnly=fields[1] == "FALSE",
                )
            )
    else:
        if CONTROL.search(raw):
            raise ValueError("Cookie header must be a single line")
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        rows = []
        for item in raw.split(";"):
            if not item.strip():
                continue
            name, separator, value = item.strip().partition("=")
            if not separator:
                raise ValueError("Use name=value pairs, separated by semicolons")
            rows.append(dict(name=name, value=value, url=origin + "/"))
    if not rows or len(rows) > 1000:
        raise ValueError("Provide between 1 and 1000 cookies")
    cookies, seen, expired = [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid Cookie entry")
        name, value = row.get("name"), row.get("value")
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(value, str):
            raise ValueError("Invalid Cookie name or value")
        if CONTROL.search(value) or len(value.encode()) > 16384:
            raise ValueError("Invalid Cookie value")
        cookie = {"name": name, "value": value}
        domain = row.get("domain")
        if domain is not None:
            if not isinstance(domain, str) or not allowed_domain(provider, domain):
                raise ValueError("Cookie domain does not belong to the selected hotel group")
            domain = domain.lower()
            if row.get("hostOnly") is True:
                domain = domain.removeprefix(".")
            path = row.get("path", "/")
            if not isinstance(path, str) or not path.startswith("/") or CONTROL.search(path):
                raise ValueError("Invalid Cookie path")
            cookie.update(domain=domain, path=path)
        else:
            cookie["url"] = official_origin(provider, row.get("url", origin).rstrip("/")) + "/"
        for key in ("httpOnly", "secure"):
            if key in row:
                if not isinstance(row[key], bool):
                    raise ValueError("Cookie flags must be boolean")
                cookie[key] = row[key]
        same_site = row.get("sameSite")
        if same_site:
            same_site = {
                "strict": "Strict",
                "lax": "Lax",
                "none": "None",
                "no_restriction": "None",
                "unspecified": None,
            }.get(str(same_site).lower(), "INVALID")
            if same_site == "INVALID":
                raise ValueError("Invalid Cookie SameSite setting")
            if same_site:
                cookie["sameSite"] = same_site
            if same_site == "None":
                cookie["secure"] = True
        expiry = row.get("expires", row.get("expirationDate"))
        if expiry is not None:
            try:
                expiry = float(expiry)
            except (TypeError, ValueError):
                raise ValueError("Cookie expiry must be a Unix timestamp") from None
            if not math.isfinite(expiry) or expiry > 253402300799:
                raise ValueError("Invalid Cookie expiry")
            if expiry > 0:
                if expiry <= now:
                    expired += 1
                    continue
                cookie["expires"] = expiry
        identity = cookie_identity(cookie)
        if identity in seen:
            raise ValueError("Duplicate Cookie name, domain and path")
        seen.add(identity)
        cookies.append(cookie)
    if not cookies:
        raise ValueError("All supplied cookies have already expired")
    return origin, cookies, expired


def cookie_identity(cookie):
    return (cookie["name"], cookie.get("domain") or urlsplit(cookie["url"]).hostname, cookie.get("path", "/"))


def connection_path(session_dir, provider):
    if provider not in DOMAINS:
        raise ValueError("Unsupported hotel group")
    return Path(session_dir) / ".connections" / (provider + ".json")


def load_connection(session_dir, provider):
    if not session_dir or provider not in DOMAINS:
        return None
    path = connection_path(session_dir, provider)
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Invalid private connection path")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("cookies"), list)
            or not isinstance(value.get("revision"), str)
            or not isinstance(value.get("enabled"), bool)
        ):
            raise ValueError("Invalid connection")
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise ValueError("Saved official connection could not be read; save it again") from None


def _save_connection(session_dir, provider, origin, raw=None, enabled=True):
    origin = official_origin(provider, origin)
    try:
        previous = load_connection(session_dir, provider)
    except ValueError:
        previous = None  # A valid explicit save can replace an unreadable file.
    cookies, expired = ([], 0)
    if enabled:
        origin, cookies, expired = parse_cookies(provider, origin, raw)
    owned = {tuple(key) for key in (previous or {}).get("owned_keys", [])}
    owned.update(cookie_identity(cookie) for cookie in cookies)
    if len(owned) > 4000:
        raise ValueError("Too many previous Cookie scopes; contact the administrator")
    value = dict(
        revision=str(uuid4()),
        origin=origin,
        enabled=enabled,
        cookies=cookies,
        owned_keys=[list(key) for key in sorted(owned)],
        updated_at=datetime.now(timezone.utc).isoformat(),
        skipped_expired=expired,
    )
    path = connection_path(session_dir, provider)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Invalid private connection path")
    os.chmod(path.parent, 0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return value


def save_connection(session_dir, provider, origin, raw=None, enabled=True):
    if provider not in _SAVE_LOCKS:
        raise ValueError("Unsupported hotel group")
    # The supplied Compose runs one API process; serialize its concurrent writes.
    with _SAVE_LOCKS[provider]:
        return _save_connection(session_dir, provider, origin, raw, enabled)


def connection_summary(provider, value):
    value = value or {}
    now = datetime.now(timezone.utc).timestamp()
    cookies = value.get("cookies", [])
    active = [cookie for cookie in cookies if cookie.get("expires", now + 1) > now]
    expiries = [cookie["expires"] for cookie in active if cookie.get("expires")]
    configured = bool(value.get("enabled") and active)
    return dict(
        provider=provider,
        configured=configured,
        origin=value.get("origin", ORIGINS[provider]),
        status="SAVED_NOT_VERIFIED" if configured else "EXPIRED" if cookies else "NOT_CONFIGURED",
        revision=value.get("revision"),
        cookie_count=len(active),
        expired_count=len(cookies) - len(active),
        earliest_cookie_expiry=datetime.fromtimestamp(min(expiries), timezone.utc).isoformat()
        if expiries
        else None,
        has_session_cookies=any("expires" not in cookie for cookie in active),
        updated_at=value.get("updated_at"),
    )
