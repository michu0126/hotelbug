import signal
import subprocess
from unittest.mock import Mock

import pytest

from app.crawler import display


def ready_pipe(monkeypatch, data=b"100\n"):
    monkeypatch.setattr(display.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(display.select, "select", lambda *_: ([7], [], []))
    monkeypatch.setattr(display.os, "read", lambda *_: data)
    connection = Mock()
    wrapper = Mock()
    wrapper.__enter__ = Mock(return_value=connection)
    wrapper.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(display.socket, "socket", lambda *_: wrapper)
    return connection


def test_ready_uses_new_number_and_live_socket(monkeypatch):
    connection = ready_pipe(monkeypatch)
    assert display.wait_display(Mock(poll=Mock(return_value=None)), 7) == ":100"
    connection.connect.assert_called_once_with("/tmp/.X11-unix/X100")


@pytest.mark.parametrize("data", [b"", b"bad\n", b"99\n100\n", b"65536\n", b"123456\n", b"1" * 17])
def test_rejects_missing_or_invalid_readiness(monkeypatch, data):
    ready_pipe(monkeypatch, data)
    with pytest.raises(RuntimeError):
        display.wait_display(Mock(poll=Mock(return_value=None)), 7)


def test_dead_xvfb_is_not_ready_even_with_stale_socket(monkeypatch):
    connection = ready_pipe(monkeypatch)
    with pytest.raises(RuntimeError):
        display.wait_display(Mock(poll=Mock(return_value=1)), 7)
    connection.connect.assert_not_called()


def test_socket_refusal_cannot_start_worker(monkeypatch):
    connection = ready_pipe(monkeypatch)
    connection.connect.side_effect = ConnectionRefusedError()
    with pytest.raises(ConnectionRefusedError):
        display.wait_display(Mock(poll=Mock(return_value=None)), 7)


def test_exit_between_readiness_and_connect_is_rejected(monkeypatch):
    ready_pipe(monkeypatch)
    with pytest.raises(RuntimeError):
        display.wait_display(Mock(poll=Mock(side_effect=[None, 1])), 7)


def test_readiness_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(display.time, "monotonic", Mock(side_effect=[0, 16]))
    with pytest.raises(RuntimeError):
        display.wait_display(Mock(poll=Mock(return_value=None)), 7, timeout=15)


def test_startup_cancellation_does_not_wait_for_deadline(monkeypatch):
    connection = ready_pipe(monkeypatch)
    assert display.wait_display(Mock(poll=Mock(return_value=None)), 7, cancelled=lambda: True) is None
    connection.connect.assert_not_called()


def setup_supervisor(monkeypatch, display_polls, worker_polls):
    xvfb = Mock(poll=Mock(side_effect=display_polls))
    worker = Mock(poll=Mock(side_effect=worker_polls))
    popen = Mock(side_effect=[xvfb, worker])
    monkeypatch.setattr(display.subprocess, "Popen", popen)
    monkeypatch.setattr(display.os, "pipe", lambda: (7, 8))
    close = Mock()
    monkeypatch.setattr(display.os, "close", close)
    monkeypatch.setattr(display, "wait_display", lambda *_, **__: ":101")
    handlers = {}

    def signal_handler(sig, handler):
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(display.signal, "signal", signal_handler)
    return xvfb, worker, popen, close, handlers


def test_worker_receives_new_display_and_exit_code_is_preserved(monkeypatch):
    xvfb, worker, popen, close, handlers = setup_supervisor(monkeypatch, [None, None], [7, 7])
    monkeypatch.setenv("DISPLAY", ":99")
    assert display.supervise(["python", "-m", "app.crawler.worker"]) == 7
    assert popen.call_args.kwargs["env"]["DISPLAY"] == ":101"
    assert "-nolisten" in popen.call_args_list[0].args[0]
    assert "-ac" not in popen.call_args_list[0].args[0]
    xvfb.terminate.assert_called_once()
    worker.terminate.assert_not_called()
    assert [call.args[0] for call in close.call_args_list] == [8, 7]
    assert handlers[signal.SIGTERM] == signal.SIG_DFL


def test_display_death_stops_worker_and_returns_failure(monkeypatch):
    xvfb, worker, *_ = setup_supervisor(monkeypatch, [1, 1], [None])
    assert display.supervise(["worker"]) == 1
    worker.terminate.assert_called_once()
    xvfb.terminate.assert_not_called()


def test_sigterm_is_forwarded_and_both_children_reaped(monkeypatch):
    xvfb, worker, _, _, handlers = setup_supervisor(monkeypatch, [None, None], [None, None])
    order = []
    worker.terminate.side_effect = lambda: order.append("worker")
    xvfb.terminate.side_effect = lambda: order.append("display")
    monkeypatch.setattr(display.time, "sleep", lambda _: handlers[signal.SIGTERM](None, None))
    assert display.supervise(["worker"]) == 0
    assert order == ["worker", "display"]
    worker.wait.assert_called_once_with(timeout=15)
    xvfb.wait.assert_called_once_with(timeout=3)


def test_startup_failure_never_launches_worker_and_cleans_fds(monkeypatch):
    xvfb, worker, popen, close, _ = setup_supervisor(monkeypatch, [None], [])
    monkeypatch.setattr(display, "wait_display", Mock(side_effect=RuntimeError("not ready")))
    with pytest.raises(RuntimeError):
        display.supervise(["worker"])
    assert popen.call_count == 1
    xvfb.terminate.assert_called_once()
    worker.terminate.assert_not_called()
    assert [call.args[0] for call in close.call_args_list] == [8, 7]


def test_failed_spawn_closes_both_fds(monkeypatch):
    _, _, popen, close, _ = setup_supervisor(monkeypatch, [], [])
    popen.side_effect = FileNotFoundError()
    with pytest.raises(FileNotFoundError):
        display.supervise(["worker"])
    assert [call.args[0] for call in close.call_args_list] == [7, 8]


def test_sigterm_during_startup_reaps_display_without_starting_worker(monkeypatch):
    xvfb, worker, popen, _, handlers = setup_supervisor(monkeypatch, [None], [])

    def interrupted_wait(*_, **__):
        handlers[signal.SIGTERM](None, None)
        return None

    monkeypatch.setattr(display, "wait_display", interrupted_wait)
    assert display.supervise(["worker"]) == 0
    assert popen.call_count == 1
    xvfb.terminate.assert_called_once()
    worker.terminate.assert_not_called()


def test_unresponsive_child_is_killed_after_grace():
    process = Mock(poll=Mock(return_value=None))
    process.wait.side_effect = [subprocess.TimeoutExpired("child", 20), 0]
    display.stop_process(process, timeout=20)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_main_only_emits_fixed_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(display, "supervise", Mock(side_effect=OSError("sensitive command")))
    assert display.main() == 1
    assert capsys.readouterr().err == "Worker display supervisor failed\n"


def test_empty_command_cannot_start_display():
    with pytest.raises(RuntimeError):
        display.supervise([])
