#!/usr/bin/env python3
"""
serve.py -- serve the dashboard locally exactly as GitHub Pages will serve it.

The page lives in docs/ and its data in data/, and at deploy time the two are
assembled into one directory. This does the same assembly virtually, so a relative
fetch of data/index.json resolves the same way locally as in production and nobody
has to remember a different path for dev.

    ./serve.py                 # http://localhost:8731
    ./serve.py --port 9000

A symlink from docs/data to ../data would also work, but not on every platform and
not without leaking into git status, so mapping the request is the tidier option.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "docs")
DATA = os.path.join(HERE, "data")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves docs/, with /data/... mapped to the repo-root data directory."""

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/data/"):
            rel = clean[len("/data/"):]
            # normalise first, then confine: a request for /data/../../etc must not escape
            full = os.path.normpath(os.path.join(DATA, rel))
            if os.path.commonpath([full, DATA]) == DATA:
                return full
        return super().translate_path(path)

    def end_headers(self):
        # the data files change under a running server; don't let the browser cache them
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "?" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8731)
    a = ap.parse_args()
    if not os.path.isdir(DATA):
        print(f"note: {DATA} does not exist yet -- run build_data.py first")

    handler = functools.partial(Handler, directory=SITE)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", a.port), handler) as srv:
        print(f"docs/ + data/ -> http://localhost:{a.port}  (ctrl-c to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
