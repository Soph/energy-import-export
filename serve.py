#!/usr/bin/env python3
"""
serve.py -- serve the dashboard locally, the way GitHub Pages serves it.

index.html and data/ both sit at the repo root, so the page's relative fetch of
data/index.json resolves without any mapping and this is a thin wrapper over the
stdlib server. It exists for two reasons worth keeping:

  * it binds to localhost, where http.server binds every interface -- serving the
    repo root over the local network would hand out .git and anything else here
  * it refuses dotted paths anyway, and sends no-store, because the data files
    change under a running server

    ./serve.py                 # http://localhost:8731
    ./serve.py --port 9000
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    """The repo root, minus anything hidden."""

    def do_GET(self):
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        if any(part.startswith(".") for part in clean.split("/") if part):
            self.send_error(404)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8731)
    a = ap.parse_args()
    if not os.path.isdir(os.path.join(HERE, "data")):
        print("note: data/ does not exist yet -- run build_data.py first")

    handler = functools.partial(Handler, directory=HERE)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", a.port), handler) as srv:
        print(f"http://localhost:{a.port}  (ctrl-c to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
