import socket
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from app.core.healthcheck import check, heartbeat_exists


@contextmanager
def redis_server(replies):
    commands = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for reply in replies:
                count = int(self.rfile.readline()[1:])
                parts = []
                for _ in range(count):
                    size = int(self.rfile.readline()[1:])
                    parts.append(self.rfile.read(size))
                    assert self.rfile.read(2) == b"\r\n"
                commands.append(parts)
                self.wfile.write(reply)
                self.wfile.flush()

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            yield f"redis://127.0.0.1:{server.server_address[1]}", commands
        finally:
            thread.join(timeout=2)


@pytest.mark.parametrize("role", ["worker", "scheduler"])
@pytest.mark.parametrize("reply,expected", [(b":1\r\n", True), (b":0\r\n", False)])
def test_checks_actual_redis_heartbeat(role, reply, expected):
    with redis_server([reply]) as (url, commands):
        assert heartbeat_exists(role, url) is expected
        assert commands == [[b"EXISTS", ("hotelbug:heartbeat:" + role).encode()]]


def test_password_auth_and_database():
    with redis_server([b"+OK\r\n", b"+OK\r\n", b":1\r\n"]) as (url, commands):
        assert heartbeat_exists("worker", url.replace("redis://", "redis://:a%40b%3Ac@") + "/2")
        assert commands == [[b"AUTH", b"a@b:c"], [b"SELECT", b"2"], [b"EXISTS", b"hotelbug:heartbeat:worker"]]


def test_acl_auth():
    with redis_server([b"+OK\r\n", b":1\r\n"]) as (url, commands):
        assert heartbeat_exists("scheduler", url.replace("redis://", "redis://user:password@"))
        assert commands[0] == [b"AUTH", b"user", b"password"]


@pytest.mark.parametrize("reply", [b"-NOAUTH private\r\n", b":2\r\n", b"+OK\r\n", b":1", b"x" * 128])
def test_rejects_invalid_or_error_response(reply):
    with redis_server([reply]) as (url, _):
        with pytest.raises(ValueError):
            heartbeat_exists("worker", url)


@pytest.mark.parametrize(
    "url", ["http://localhost", "redis:///0", "redis://localhost/-1", "redis://localhost/no-db"]
)
def test_rejects_invalid_configuration_without_connecting(url, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid configuration must not connect")

    monkeypatch.setattr(socket, "create_connection", unexpected)
    with pytest.raises(ValueError):
        heartbeat_exists("worker", url)


def test_socket_timeout_is_propagated(monkeypatch):
    def timed_out(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(socket, "create_connection", timed_out)
    with pytest.raises(TimeoutError):
        heartbeat_exists("worker", "redis://localhost")


def test_deployed_probe_does_not_import_application_or_redis(monkeypatch):
    with redis_server([b":1\r\n"]) as (url, _):
        monkeypatch.setenv("REDIS_URL", url)
        assert check("worker") is True
    source = "import sys; import app.core.healthcheck; assert 'redis' not in sys.modules; assert 'pydantic' not in sys.modules"
    subprocess.run([sys.executable, "-c", source], check=True, timeout=10)


def test_cli_fails_without_leaking_redis_secrets(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "https://secret-user:secret-password@localhost")
    result = subprocess.run(
        [sys.executable, "-m", "app.core.healthcheck", "worker"], capture_output=True, timeout=10
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
