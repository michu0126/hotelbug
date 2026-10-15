"""Private Xvfb/Worker lifecycle. Only stdlib imports, no application settings."""

import os
import select
import signal
import socket
import subprocess
import sys
import time


def stop_process(process, timeout=5):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def wait_display(process, read_fd, timeout=45, cancelled=None):
    """Xserver writes its newly allocated display number only when listening."""
    deadline = time.monotonic() + timeout
    received = b""
    while process.poll() is None:
        if cancelled is not None and cancelled():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not select.select([read_fd], [], [], min(remaining, 0.1))[0]:
            continue
        chunk = os.read(read_fd, 64)
        if not chunk:
            break
        received += chunk
        if b"\n" not in received:
            if len(received) > 16:
                break
            continue
        number = received.strip()
        if not number.isdigit() or len(number) > 5 or int(number) > 65535:
            break
        # A leftover X99 socket cannot satisfy readiness for this new process.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(remaining, 1))
            connection.connect(f"/tmp/.X11-unix/X{int(number)}")
        if process.poll() is None:
            return f":{int(number)}"
        break
    raise RuntimeError("Worker virtual display failed to become ready")


def supervise(command):
    if not command:
        raise RuntimeError("Worker command is required")
    display = worker = None
    read_fd, write_fd = os.pipe()
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        display = subprocess.Popen(
            [
                "Xvfb",
                "-displayfd",
                str(write_fd),
                "-screen",
                "0",
                "1440x1000x24",
                "-nolisten",
                "tcp",
                "-noreset",
            ],
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        write_fd = None
        selected_display = wait_display(display, read_fd, cancelled=lambda: stopping)
        if stopping:
            return 0
        if selected_display is None:
            raise RuntimeError("Worker virtual display was not selected")
        worker = subprocess.Popen(command, env={**os.environ, "DISPLAY": selected_display})
        while not stopping:
            if display.poll() is not None:
                print("Worker virtual display exited; stopping Worker", file=sys.stderr, flush=True)
                return 1
            result = worker.poll()
            if result is not None:
                return result if result >= 0 else 128 - result
            time.sleep(0.1)
        return 0
    finally:
        # Keep the display until the Worker stops. Stay inside Compose's 30s
        # stop grace, even if both children need killing.
        try:
            if worker is not None:
                stop_process(worker, timeout=15)
        finally:
            if display is not None:
                stop_process(display, timeout=3)
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    try:
        return supervise(sys.argv[1:])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Never print command arguments, environment values or raw exceptions.
        print("Worker display supervisor failed", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
