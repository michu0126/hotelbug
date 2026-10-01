#!/bin/sh
set -eu

# Give the ordinary (non-headless) browser a private display inside this
# container. No desktop, VNC, or additional network port is exposed to the NAS.
export DISPLAY="${DISPLAY:-:99}"
display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
Xvfb "$DISPLAY" -screen 0 1440x1000x24 -nolisten tcp -noreset &
display_pid=$!
display_attempts=0
while [ ! -S "/tmp/.X11-unix/X${display_number}" ]; do
    if ! kill -0 "$display_pid" 2>/dev/null; then
        echo "Worker virtual display failed to start" >&2
        exit 1
    fi
    display_attempts=$((display_attempts + 1))
    if [ "$display_attempts" -ge 50 ]; then
        echo "Worker virtual display startup timed out" >&2
        exit 1
    fi
    sleep 0.1
done

# Keep the actual Worker as the main process so shutdown and job cancellation
# remain effective; the display process exits with the container.
exec "$@"
