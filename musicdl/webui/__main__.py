"""``python -m musicdl.webui [--demo] [--no-window] [--port N]``"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import urllib.request
from pathlib import Path
from typing import List, Optional

from musicdl.webui import server, system

PREFERRED_PORT = 47651
INSTANCE_FILE = server.APP_DIR / "webui.json"
PROFILE_DIR = server.APP_DIR / "webui-profile"


def setup_logging() -> None:
    """pythonw.exe has no console: send output and logs to ~/.musicdl/log.txt."""
    server.APP_DIR.mkdir(parents=True, exist_ok=True)
    log = open(server.LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    if sys.stdout is None or sys.stderr is None or "pythonw" in Path(sys.executable).name.lower():
        sys.stdout = log
        sys.stderr = log
    logging.basicConfig(
        stream=log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("spotdl", "yt_dlp", "urllib3", "ytmusicapi"):
        logging.getLogger(name).setLevel(logging.WARNING)


def running_instance() -> Optional[dict]:
    """Another copy of the window is already running: -> its port and token."""
    try:
        info = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/api/hello", timeout=1.5) as response:
            if json.load(response).get("app") == "musicdl-webui":
                return info
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def window_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?t={token}"


def make_engine(args: argparse.Namespace):
    if args.demo:
        from musicdl.webui.demo import DemoEngine

        return DemoEngine(speed=args.demo_speed)
    from musicdl.webui.engine import RealEngine

    return RealEngine()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="musicdl.webui", description="New musicdl window")
    parser.add_argument("--demo", action="store_true", help="pretend engine: no network, spotDL or ffmpeg needed")
    parser.add_argument("--demo-speed", type=float, default=1.0, help="speed up the demo (e.g. 5)")
    parser.add_argument("--no-window", action="store_true", help="only start the server and print its address")
    parser.add_argument("--port", type=int, default=0, help="port (default: %d, or any free one)" % PREFERRED_PORT)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    setup_logging()
    if not args.demo and not args.no_window:
        instance = running_instance()
        if instance:  # one copy at a time: just show its window again
            system.open_app_window(window_url(int(instance["port"]), str(instance["token"])), PROFILE_DIR)
            return 0

    app = server.App(make_engine(args))
    if args.demo:
        from musicdl.webui import demo

        app.picker = lambda kind, initial: (
            str(demo.write_demo_csv(server.APP_DIR / "demo" / "playlist.csv")) if kind == "csv" else initial or str(server.DEFAULT_OUT)
        )
    httpd = None
    for port in (args.port or PREFERRED_PORT, 0):
        try:
            httpd = server.AppServer(app, port)
            break
        except OSError:
            continue
    if httpd is None:
        print("Не удалось открыть порт для окна программы.", file=sys.stderr)
        return 1

    port = httpd.server_address[1]
    url = window_url(port, app.token)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="webui-http").start()
    if not args.demo:
        INSTANCE_FILE.write_text(json.dumps({"port": port, "token": app.token, "pid": os.getpid()}), encoding="utf-8")
    logging.getLogger("musicdl").info("webui started on port %s (%s engine)", port, app.engine.name)
    if args.no_window:
        print(url, flush=True)
    else:
        system.open_app_window(url, PROFILE_DIR)
    try:
        reason = httpd.wait_until_window_closed()
        logging.getLogger("musicdl").info("webui stopping: %s", reason)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            INSTANCE_FILE.unlink()
        except OSError:
            pass
        httpd.shutdown_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
