"""Probe Redis using stdlib only: full app imports exceed NAS health timeouts."""

import os
import socket
import sys
import time
from urllib.parse import unquote, urlsplit


def heartbeat_exists(role: str, redis_url: str, timeout: float = 3.0) -> bool:
    if role not in {"worker", "scheduler"} or timeout <= 0:
        raise ValueError("Invalid heartbeat probe")
    url = urlsplit(redis_url)
    if url.scheme not in {"redis", "rediss"} or not url.hostname:
        raise ValueError("Invalid Redis configuration")
    database = int(url.path.lstrip("/") or "0")
    if database < 0:
        raise ValueError("Invalid Redis database")
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Heartbeat probe deadline exceeded")
        return value

    connection = socket.create_connection((url.hostname, url.port or 6379), timeout=remaining())
    try:
        if url.scheme == "rediss":
            import ssl

            connection.settimeout(remaining())
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=url.hostname)
        with connection.makefile("rb") as stream:

            def command(*parts):
                values = [str(part).encode("utf-8") for part in parts]
                data = (
                    b"*"
                    + str(len(values)).encode()
                    + b"\r\n"
                    + b"".join(
                        b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n" for value in values
                    )
                )
                connection.settimeout(remaining())
                connection.sendall(data)
                connection.settimeout(remaining())
                reply = stream.readline(128)
                if not reply.endswith(b"\r\n") or reply.startswith(b"-"):
                    raise ValueError("Redis heartbeat command failed")
                return reply

            if url.password is not None or url.username:
                password = unquote(url.password or "")
                reply = (
                    command("AUTH", unquote(url.username), password)
                    if url.username
                    else command("AUTH", password)
                )
                if reply != b"+OK\r\n":
                    raise ValueError("Redis heartbeat authentication failed")
            if database and command("SELECT", database) != b"+OK\r\n":
                raise ValueError("Redis heartbeat database selection failed")
            reply = command("EXISTS", "hotelbug:heartbeat:" + role)
            if reply not in {b":0\r\n", b":1\r\n"}:
                raise ValueError("Invalid Redis heartbeat response")
            return reply == b":1\r\n"
    finally:
        connection.close()


def check(role: str) -> bool:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url is None:
        # Local .env remains supported; Compose always sets REDIS_URL explicitly.
        from app.core.config import get_settings

        redis_url = get_settings().redis_url.get_secret_value()
    return heartbeat_exists(role, redis_url)


if __name__ == "__main__":
    try:
        healthy = len(sys.argv) == 2 and check(sys.argv[1])
    except Exception:
        # Never expose authenticated Redis URLs or raw server errors in Docker.
        healthy = False
    raise SystemExit(0 if healthy else 1)
