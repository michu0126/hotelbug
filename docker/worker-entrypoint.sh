#!/bin/sh
set -eu

# Wait for Xvfb's readiness pipe, not a possibly stale filesystem socket.
# Supervise both processes so a dead display cannot leave a healthy-looking
# Worker producing repeated local browser errors. No network display is exposed.
exec python -m app.crawler.display "$@"
