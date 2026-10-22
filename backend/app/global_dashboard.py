"""Small read-only view of the local worldwide collector's durable progress."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def start_dashboard(config):
    from app.local_global import report

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                body = json.dumps(report(config), ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                body = Path(__file__).with_name("global_dashboard.html").read_bytes()
                mime = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", config.dashboard_port), Handler)
    worker = threading.Thread(target=server.serve_forever, name="global-dashboard", daemon=True)
    worker.start()
    return server
