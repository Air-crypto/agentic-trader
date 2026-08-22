#!/usr/bin/env python3
"""Serve the trading knowledge graph and its dependency-free viewer locally."""

from __future__ import annotations

import argparse
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_graph.py"


class GraphHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path in {"/", "/viewer"}:
            self.send_response(302)
            self.send_header("Location", "/viewer/")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/regenerate":
            self.send_error(404)
            return
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        response = (result.stdout + result.stderr).encode("utf-8")
        self.send_response(200 if result.returncode == 0 else 422)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), GraphHandler)
    print(f"viewer: http://{args.host}:{args.port}/viewer/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
