"""Dev server for the web/ UI (stdlib only).

Serves web/ as static files on http://127.0.0.1:8778 and proxies /api/* to the
FastAPI app on 127.0.0.1:8777.

    ~/.local/bin/uv run python web/dev_server.py        # PORT=…  API=http://127.0.0.1:8777
"""
from __future__ import annotations

import http.client
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8778"))
API = urlsplit(os.environ.get("API", "http://127.0.0.1:8777"))
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host"}
CHUNK = 1024 * 1024


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                      ".mjs": "text/javascript", ".woff2": "font/woff2", ".json": "application/json"}

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _proxy(self):
        conn = http.client.HTTPConnection(API.hostname, API.port or 80, timeout=600)
        length = int(self.headers.get("Content-Length") or 0)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"{API.hostname}:{API.port}")
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            left = length
            while left > 0:  # stream uploads instead of buffering 300 MB
                buf = self.rfile.read(min(CHUNK, left))
                if not buf:
                    break
                conn.send(buf)
                left -= len(buf)
            res = conn.getresponse()
            self.send_response(res.status, res.reason)
            for k, v in res.getheaders():
                if k.lower() not in HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            body = res.read()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            msg = f'{{"detail": "API at {API.geturl()} is not reachable: {exc}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    def _route(self):
        if self.path.startswith("/api/") or self.path == "/openapi.json":
            return self._proxy()
        if self.command in ("GET", "HEAD"):
            return super().do_GET() if self.command == "GET" else super().do_HEAD()
        self.send_error(405)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = _route

    def log_message(self, fmt, *args):
        if "/api/jobs/" in (args[0] if args else "") and '" 200' in fmt % args:
            return  # keep polling quiet
        super().log_message(fmt, *args)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT)))
    print(f"web/ on http://127.0.0.1:{PORT}  (api -> {API.geturl()})")
    srv.serve_forever()
