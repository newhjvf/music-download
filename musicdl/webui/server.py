"""Local web server behind the new window (standard library only).

* ``GET /``                 the page (``static/``)
* ``GET /api/events``       Server-Sent Events: a snapshot, then patches (see ``session.py``)
* ``POST /api/<name>``      actions: start, stop, settings, pick, open, ...

It listens on 127.0.0.1 only. Because any web page in the user's browser can
send requests to 127.0.0.1, every ``/api`` call must carry a random token
that is handed to our own window in its URL, and the ``Host`` header must be
ours (against DNS rebinding).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from musicdl.webui import system
from musicdl.webui.session import Session

logger = logging.getLogger(__name__)

APP_DIR = Path.home() / ".musicdl"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "log.txt"
IMPORT_DIR = APP_DIR / "imports"
DEFAULT_OUT = Path.home() / "Music" / "musicdl"
STATIC_DIR = Path(__file__).resolve().parent / "static"

BITRATES = ["128k", "192k", "256k", "320k"]
SETTING_KEYS = ("mode", "csv_path", "link", "out_dir", "bitrate", "threads", "only_verified", "theme")
MAX_UPLOAD = 20 * 1024 * 1024
MAX_BODY = 64 * 1024

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def clamp_threads(value: Any, default: int = 4) -> int:
    try:
        return min(8, max(1, int(value)))
    except (TypeError, ValueError):
        return default


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class App:
    """What the window can ask for. Each ``api_*`` takes the decoded JSON body
    and returns a JSON-able dict, so it can be tested without HTTP."""

    def __init__(
        self,
        engine: Any,
        token: Optional[str] = None,
        opener: Optional[Callable[[Path], None]] = None,
        url_opener: Optional[Callable[[str], None]] = None,
        picker: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> None:
        self.engine = engine
        self.session = Session(engine)
        self.token = token or secrets.token_urlsafe(24)
        self.opener = opener or system.open_path
        self.url_opener = url_opener or system.open_url
        self.picker = picker or system.pick_path

    # ------------------------------------------------------------- settings
    def load_settings(self) -> Dict[str, Any]:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def save_settings(self, changes: Dict[str, Any]) -> None:
        """Merge ``changes`` into settings.json; keys written by the other
        window (or newer versions) are kept."""
        data = self.load_settings()
        data.update({key: value for key, value in changes.items() if key in SETTING_KEYS})
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except OSError:
            logger.warning("Could not save settings", exc_info=True)

    def form_defaults(self) -> Dict[str, Any]:
        saved = self.load_settings()
        return {
            "mode": saved.get("mode", "csv") if saved.get("mode") in ("csv", "url") else "csv",
            "csv_path": str(saved.get("csv_path", "")),
            "link": str(saved.get("link", "")),
            "out_dir": str(saved.get("out_dir", DEFAULT_OUT)),
            "bitrate": saved.get("bitrate") if saved.get("bitrate") in BITRATES else "320k",
            "threads": clamp_threads(saved.get("threads")),
            "only_verified": bool(saved.get("only_verified", False)),
            "theme": saved.get("theme") if saved.get("theme") in ("dark", "light") else "",
        }

    # ------------------------------------------------------------------ api
    def api_hello(self, _body: Dict[str, Any]) -> Dict[str, Any]:
        return {"app": "musicdl-webui"}

    def api_config(self, _body: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "version": self.engine.version(),
            "engine": self.engine.name,
            "form": self.form_defaults(),
            "bitrates": BITRATES,
            "logPath": str(LOG_FILE),
            "windows": os.name == "nt",
        }

    def api_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        self.save_settings(body)
        return {"ok": True}

    def api_validate_link(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind = self.engine.check_link(str(body.get("link", "")))
        return {"ok": kind is not None, "kind": kind}

    def validate(self, body: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
        """-> (message for the user or None, engine options). Same rules as the old window."""
        mode = body.get("mode")
        source = str(body.get("csv_path" if mode == "csv" else "link", "")).strip()
        if mode == "csv":
            if not source:
                return "Выберите CSV-файл: перетащите его в окно или нажмите «Выбрать файл».", {}
            if not Path(source).is_file():
                return f"Файл не найден:\n{source}", {}
        elif mode == "url":
            if self.engine.check_link(source) is None:
                return (
                    "Это не похоже на ссылку Spotify на трек, альбом или плейлист.\n"
                    "Скопируйте её в Spotify: «Поделиться → Копировать ссылку».",
                    {},
                )
        else:
            return "Выберите, откуда брать треки.", {}
        out_dir = str(body.get("out_dir", "")).strip()
        if not out_dir:
            return "Выберите папку для музыки.", {}
        try:
            threads = int(body.get("threads", 4))
            if not 1 <= threads <= 8:
                raise ValueError
        except (TypeError, ValueError):
            return "«Одновременно» — число от 1 до 8.", {}
        bitrate = body.get("bitrate") if body.get("bitrate") in BITRATES else "320k"
        return None, {
            "source_kind": mode,
            "source": source,
            "out_dir": out_dir,
            "threads": threads,
            "bitrate": bitrate,
            "dry_run": bool(body.get("dry_run")),
            "only_verified": bool(body.get("only_verified")),
        }

    def api_start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.session.running:
            return {"ok": False, "message": "Работа уже идёт."}
        problem, options = self.validate(body)
        if problem:
            return {"ok": False, "message": problem}
        self.save_settings({key: body.get(key) for key in SETTING_KEYS if key in body})
        self.session.start(options)
        return {"ok": True}

    def api_stop(self, _body: Dict[str, Any]) -> Dict[str, Any]:
        self.session.stop()
        return {"ok": True}

    def api_pick(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind = "csv" if body.get("kind") == "csv" else "folder"
        try:
            path = self.picker(kind, str(body.get("initial", "")))
        except RuntimeError as exc:
            return {"ok": False, "message": f"{exc}. Вставьте путь в поле вручную."}
        return {"ok": True, "path": path}

    def api_open(self, body: Dict[str, Any]) -> Dict[str, Any]:
        what = body.get("what")
        try:
            if what == "row":
                link = self.session.link_for(int(body.get("row", -1)))
                if not link:
                    return {"ok": False, "message": "У этого трека нет ссылки."}
                self.url_opener(link)
                return {"ok": True}
            if what == "out":
                out = Path(str(body.get("out_dir") or self.session.out_dir or DEFAULT_OUT))
                out.mkdir(parents=True, exist_ok=True)
                self.opener(out)
                return {"ok": True}
            path = Path(self.session.report) if what == "report" and self.session.report else LOG_FILE if what == "log" else None
            if path is None or not path.exists():
                return {"ok": False, "message": f"Файла пока нет:\n{path}" if path else "Файла пока нет."}
            self.opener(path)
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "message": f"Не удалось открыть: {exc}"}

    def save_upload(self, name: str, data: bytes) -> Dict[str, Any]:
        """A CSV dropped onto the window: the browser gives no path, so keep a copy."""
        stem = "".join(ch for ch in Path(name).stem if ch.isalnum() or ch in " ._-()").strip() or "playlist"
        IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = IMPORT_DIR / f"{stem[:80]}.csv"
        target.write_bytes(data)
        return {"ok": True, "path": str(target)}

    def state(self) -> Dict[str, Any]:
        snapshot = self.session.snapshot()
        snapshot["config"] = self.api_config({})
        return snapshot


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "musicdl-webui"
    server: "AppServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from the base class
        logger.debug("%s - %s", self.address_string(), format % args)

    # -- helpers ---------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Dict[str, Any]) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _allowed(self, query: Dict[str, Any]) -> bool:
        if not self._host_ok():
            return False
        token = self.headers.get("X-Musicdl-Token") or (query.get("t") or [""])[0]
        return hmac.compare_digest(token, self.server.app.token)

    # -- GET ---------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        if url.path == "/api/hello":
            return self._json(200, self.server.app.api_hello({}))
        if url.path.startswith("/api/"):
            if not self._allowed(query):
                return self._json(403, {"error": "forbidden"})
            if url.path == "/api/events":
                return self._events()
            if url.path == "/api/state":
                return self._json(200, self.server.app.state())
            if url.path == "/api/config":
                return self._json(200, self.server.app.api_config({}))
            return self._json(404, {"error": "not found"})
        name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        self._send(200, path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def _events(self) -> None:
        session = self.server.app.session
        channel = session.subscribe()
        self.server.client_connected()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            idle = 0
            while not self.server.stopping:
                try:
                    message = channel.get(timeout=1.0)
                except queue.Empty:
                    idle += 1
                    if self._peer_closed():
                        break
                    if idle % 10 == 0:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    continue
                idle = 0
                data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            session.unsubscribe(channel)
            self.server.client_gone()

    def _peer_closed(self) -> bool:
        """The window was closed: the socket becomes readable and says "EOF"."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # -- POST --------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not url.path.startswith("/api/") or not self._allowed(query):
            return self._json(403, {"error": "forbidden"})
        name = url.path[len("/api/"):]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if name == "upload":
                if length > MAX_UPLOAD:
                    raise ApiError("Файл слишком большой для CSV.", 413)
                data = self.rfile.read(length)
                return self._json(200, self.server.app.save_upload((query.get("name") or ["playlist.csv"])[0], data))
            if length > MAX_BODY:
                raise ApiError("Слишком большой запрос.", 413)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ApiError("Ожидался объект JSON.")
            action = getattr(self.server.app, f"api_{name}", None)
            if name in ("hello", "config") or not callable(action):
                raise ApiError("Неизвестная команда.", 404)
            return self._json(200, action(body))
        except ApiError as exc:
            return self._json(exc.status, {"ok": False, "message": str(exc)})
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "message": "Некорректный запрос."})
        except Exception:  # never let a bug close the connection without an answer
            logger.error("API call %s failed", name, exc_info=True)
            return self._json(500, {"ok": False, "message": "Внутренняя ошибка, подробности в журнале."})


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy steal the port

    def __init__(self, app: App, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.app = app
        self.stopping = False
        self._lock = threading.Lock()
        self._clients = 0
        self._ever_connected = False
        self._empty_since: Optional[float] = None

    def client_connected(self) -> None:
        with self._lock:
            self._clients += 1
            self._ever_connected = True
            self._empty_since = None

    def client_gone(self) -> None:
        with self._lock:
            self._clients -= 1
            if self._clients <= 0:
                self._empty_since = time.monotonic()

    def wait_until_window_closed(self, first_window_timeout: float = 90.0, grace: float = 8.0) -> str:
        """Block until the window was closed (no event stream for ``grace``
        seconds, so a page reload does not count) or never opened."""
        started = time.monotonic()
        while True:
            time.sleep(0.25)
            now = time.monotonic()
            with self._lock:
                if self._clients <= 0 and self._empty_since is not None and now - self._empty_since > grace:
                    return "closed"
                if not self._ever_connected and now - started > first_window_timeout:
                    return "never-opened"

    def shutdown_all(self) -> None:
        self.stopping = True
        self.app.session.shutdown()
        self.shutdown()
        self.server_close()
