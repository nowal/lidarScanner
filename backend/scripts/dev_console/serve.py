"""Serve the developer console (a static page) on a local port.

The console talks to a running backend over its public API -- the same
calls the app and operations make -- so nothing here touches the
delivery code. It is a tool for walking through ingested homes and rooms
and chatting with the agent as the homeowner would, with the flow state
laid bare next to every reply.

    .venv/Scripts/python scripts/dev_console/serve.py [--port 8020]

Then open http://127.0.0.1:8020/ and point it at the backend (default
http://127.0.0.1:8010) with the ops token.

If TakeShape's single-room test fixture is checked out next to this repo
(upstream/takeshape-mobile/LidarAITests/captured-room.json) its chat
context packet is written to fixtures/ so the console can also run the
single-room path without a home index.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
FIXTURE = BACKEND.parents[1] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"


def write_single_room_context() -> None:
    if not FIXTURE.exists():
        return
    sys.path.insert(0, str(BACKEND))
    from scripts.roomplan_context import context_from_captured_room

    out = HERE / "fixtures"
    out.mkdir(exist_ok=True)
    (out / "captured-room-context.json").write_text(
        json.dumps(context_from_captured_room(FIXTURE)), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    write_single_room_context()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"dev console: http://127.0.0.1:{args.port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
