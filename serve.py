#!/usr/bin/env python3
"""
serve.py -- serve the dashboard locally, the way GitHub Pages serves it.

index.html and data/ both sit at the repo root, so the page's relative fetch of
data/index.json resolves without any mapping and this is a thin wrapper over the
stdlib server. What it adds:

  * a choice of interface. It binds loopback by default, because http.server binds
    *everything* and this serves the repo root -- but --lan opts into the local
    network, which is how you reach it from another machine.
  * dotted paths are refused whatever the binding, so .git, .env and .github are
    never reachable even when the port is open to the network.

    ./serve.py                 # http://localhost:8731, this machine only
    ./serve.py --lan           # also reachable at http://<your-lan-ip>:8731
    ./serve.py --lan --port 9000
    ./serve.py --host 0.0.0.0  # the same as --lan, spelled out
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import socket
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    """The repo root, minus anything hidden."""

    def do_GET(self):
        if self._hidden():
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        if self._hidden():
            self.send_error(404)
            return
        super().do_HEAD()

    def _hidden(self) -> bool:
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        return any(part.startswith(".") for part in clean.split("/") if part)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def lan_addresses() -> list[str]:
    """Best-effort list of addresses this machine answers on."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))          # no packets sent, just picks a route
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--host", default=None,
                    help="interface to bind (default 127.0.0.1, this machine only)")
    ap.add_argument("--lan", action="store_true",
                    help="bind every interface so other machines on the network can reach it")
    a = ap.parse_args()
    host = a.host or ("0.0.0.0" if a.lan else "127.0.0.1")

    if not os.path.isdir(os.path.join(HERE, "data")):
        print("note: data/ does not exist yet -- run build_data.py first")

    handler = functools.partial(Handler, directory=HERE)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, a.port), handler) as srv:
        print(f"  http://localhost:{a.port}")
        if host != "127.0.0.1":
            for ip in lan_addresses():
                print(f"  http://{ip}:{a.port}")
            print(f"  serving {HERE} to the network; dotted paths such as .git are refused")
        print("  ctrl-c to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
