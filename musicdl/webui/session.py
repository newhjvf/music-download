"""State of one run, shared between the engine thread and the browser.

The engine (``musicdl.job.run_job``, or the demo engine) reports progress from
worker threads. ``Session`` turns those callbacks into a compact model
(rows + counters + progress) and, a few times a second, pushes only what
changed to every connected window as a *patch*. All animation and ticking
(spinners, elapsed time, ETA) is done by the page itself, so a long list costs
the Python side almost nothing.

Nothing here imports spotDL or tkinter.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.08  # seconds: events arriving within this window are merged

# Row states (the page maps them to icons, colours and Russian labels).
#   queued searching found ready missing searcherr cancelled
#   preparing downloading converting tagging retrying done dlerror exists
STATE_BY_DOWNLOAD_MESSAGE = {
    "Searching": "preparing",
    "Getting metadata": "preparing",
    "Downloading": "downloading",
    "Converting": "converting",
    "Embedding metadata": "tagging",
}
DOWNLOAD_FINAL = {"done", "dlerror", "exists"}


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    if seconds <= 0:
        return "?"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def source_label(match: Any) -> str:
    """'YouTube Music', 'YouTube Music, видео', 'SoundCloud'..."""
    source = getattr(match, "source", "") or "YouTube Music"
    if source == "YouTube Music" and not getattr(match, "verified", False):
        return "YouTube Music, видео"
    return source


def new_row(song: Any, state: str = "queued", found: str = "") -> Dict[str, Any]:
    return {
        "a": song.artist,  # artist
        "n": song.name,  # track name
        "s": state,
        "f": found,  # found title (or expected file name for tracks that already exist)
        "fa": "",  # found author
        "src": "",  # source label
        "v": 0,  # official track (YouTube Music "song")
        "d": "",  # duration text
        "p": 0,  # download percent
        "x": "",  # detail (source being searched, error text)
        "l": 0,  # has a link to open
    }


class Session:
    """Thread-safe run state. ``engine`` runs the job; see ``engine.py``."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.lock = threading.RLock()
        self.rows: List[Dict[str, Any]] = []
        self.links: List[str] = []
        self.index: Dict[str, int] = {}  # song key -> row
        self.run_id = 0
        self.running = False
        self.dry_run = False
        self.cancel: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None
        self.report: Optional[str] = None
        self.out_dir = ""
        self.threads = 4
        self._reset_meta()

        self._pending_rows: Set[int] = set()
        self._meta_dirty = False
        self._wake = threading.Event()
        self._clients: List["queue.Queue[Dict[str, Any]]"] = []
        threading.Thread(target=self._flusher, daemon=True, name="webui-flusher").start()

    # ------------------------------------------------------------------ model
    def _reset_meta(self) -> None:
        self.phase = ""  # "" | "load" | "search" | "download" | "done"
        self.phase_done = 0
        self.phase_total = 0
        self.phase_started = 0.0
        self.started = 0.0
        self.finished = 0.0
        self.status = ""
        self.tone = "info"  # info | ok | warn | bad
        self.offline = False
        self.can_stop = False
        self.summary: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.counters = {"total": 0, "existing": 0, "found": 0, "missing": 0, "done": 0}
        self.searched = self.search_total = 0

    def meta(self) -> Dict[str, Any]:
        return {
            "now": time.time(),
            "run": self.run_id,
            "running": self.running,
            "dryRun": self.dry_run,
            "phase": self.phase,
            "done": self.phase_done,
            "total": self.phase_total,
            "phaseStart": self.phase_started,
            "start": self.started,
            "end": self.finished,
            "status": self.status,
            "tone": self.tone,
            "offline": self.offline,
            "canStop": self.can_stop,
            "counters": dict(self.counters),
            "report": bool(self.report),
            "summary": self.summary,
            "error": self.error,
        }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "t": "snapshot",
                "meta": self.meta(),
                "rows": [dict(row) for row in self.rows],
            }

    # ---------------------------------------------------------- client plumbing
    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        """A new window: its queue starts with a full snapshot, then gets patches."""
        channel: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self.lock:
            channel.put(self.snapshot())
            self._clients.append(channel)
        return channel

    def unsubscribe(self, channel: "queue.Queue[Dict[str, Any]]") -> None:
        with self.lock:
            if channel in self._clients:
                self._clients.remove(channel)

    @property
    def client_count(self) -> int:
        with self.lock:
            return len(self._clients)

    def broadcast(self, message: Dict[str, Any]) -> None:
        with self.lock:
            for channel in self._clients:
                channel.put(message)

    def toast(self, level: str, text: str) -> None:
        self.broadcast({"t": "toast", "level": level, "text": text})

    def _touch(self, index: Optional[int] = None) -> None:
        with self.lock:
            if index is not None:
                self._pending_rows.add(index)
            self._meta_dirty = True
        self._wake.set()

    def _flusher(self) -> None:
        while True:
            self._wake.wait()
            time.sleep(FLUSH_INTERVAL)
            self._wake.clear()
            self.flush()

    def flush(self) -> None:
        """Send what changed since the last flush to every window."""
        with self.lock:
            if not self._meta_dirty and not self._pending_rows:
                return
            patch = {
                "t": "patch",
                "meta": self.meta(),
                "rows": [[i, self.rows[i]] for i in sorted(self._pending_rows) if i < len(self.rows)],
            }
            self._pending_rows.clear()
            self._meta_dirty = False
            for channel in self._clients:
                channel.put(patch)

    # ------------------------------------------------------------ counters/rows
    def _count(self, name: str, delta: int = 1) -> None:
        self.counters[name] = max(0, self.counters[name] + delta)

    def _set(self, index: int, **fields: Any) -> None:
        self.rows[index].update(fields)
        self._touch(index)

    def _say(self, text: str, tone: str = "info") -> None:
        self.status, self.tone = text, tone
        self._touch()

    # ------------------------------------------------------------------- run
    def start(self, options: Dict[str, Any]) -> None:
        """Begin a run in a background thread. ``options`` is already validated."""
        with self.lock:
            if self.running:
                raise RuntimeError("already running")
            self.run_id += 1
            self.rows, self.links, self.index = [], [], {}
            self.report = None
            self._reset_meta()
            self.running = True
            self.can_stop = True
            self.dry_run = bool(options["dry_run"])
            self.out_dir = str(options["out_dir"])
            self.threads = int(options["threads"])
            self.started = time.time()
            self.phase = "load"
            self.status = "Загружаю список треков…"
            self.cancel = threading.Event()
            self.counters["total"] = -1  # shown as "…" until the list is loaded
            cancel = self.cancel
            self.broadcast(self.snapshot())
            self.worker = threading.Thread(target=self._work, args=(options, cancel), daemon=True, name="webui-job")
            self.worker.start()

    def stop(self) -> None:
        with self.lock:
            if not self.running or self.cancel is None or self.cancel.is_set():
                return
            self.cancel.set()
            self.can_stop = False
            self._say("Останавливаю: дожидаюсь треков, которые уже ищутся…", "warn")

    def _work(self, options: Dict[str, Any], cancel: threading.Event) -> None:
        handlers: Dict[str, Callable] = {
            "info": self.on_info,
            "songs_loaded": self.on_songs,
            "matched": self.on_match,
            "download_status": self.on_download,
            "phase": self.on_phase,
            "searching": self.on_searching,
            "connection": self.on_connection,
        }
        try:
            summary = self.engine.run(options, cancel, handlers)
        except Exception as exc:  # engine.user_error() tells apart expected errors from crashes
            expected = self.engine.is_user_error(exc)
            if not expected:
                logger.error("Unexpected error", exc_info=True)
            self.on_error(str(exc) if expected else f"{type(exc).__name__}: {exc}", None if expected else traceback.format_exc())
        else:
            self.on_done(summary)

    # --------------------------------------------------------- engine callbacks
    def on_info(self, message: str) -> None:
        with self.lock:
            if not self.offline:
                self._say(message)

    def on_songs(self, todo: List[Any], existing: List[Any]) -> None:
        with self.lock:
            out = self.out_dir
            for song in existing:
                self.index[song.url] = len(self.rows)
                self.rows.append(new_row(song, "exists", self.engine.existing_name(song, out)))
                self.links.append("")
            for song in todo:
                self.index[song.url] = len(self.rows)
                self.rows.append(new_row(song))
                self.links.append("")
            self.counters["total"] = len(todo) + len(existing)
            self.counters["existing"] = len(existing)
            self.broadcast(self.snapshot())

    def on_phase(self, name: str, count: int) -> None:
        with self.lock:
            self.phase, self.phase_total, self.phase_done = name, count, 0
            self.phase_started = time.time()
            if name == "search":
                self.searched, self.search_total = 0, count
            else:
                self.can_stop = False
            self._touch()

    def on_searching(self, key: str, source: str) -> None:
        with self.lock:
            i = self.index.get(key)
            if i is None:
                return
            self._set(i, s="searching", x=source)
            if not self.offline:
                row = self.rows[i]
                end = min(self.search_total, self.searched + self.threads * 2)
                self._say(f"Ищу {self.searched + 1}–{end} из {self.search_total}: «{row['a']} - {row['n']}» на {source}…")

    def on_match(self, match: Any) -> None:
        with self.lock:
            self.searched += 1
            self.phase_done += 1
            i = self.index.get(match.song.url)
            if i is None:
                self._touch()
                return
            if match.found:
                self._count("found")
                self.links[i] = match.url
                self._set(
                    i,
                    s="found" if self.dry_run else "ready",
                    f=match.title or match.url,
                    fa=match.author or "",
                    src=source_label(match),
                    v=1 if match.verified else 0,
                    d=format_duration(match.duration),
                    x="",
                    l=1,
                )
            elif self.engine.is_cancelled(match):
                self._set(i, s="cancelled", x="")
            else:
                self._count("missing")
                if self.engine.is_not_found(match):
                    self._set(i, s="missing", x="")
                else:
                    self._set(i, s="searcherr", x=match.error or "")

    def on_connection(self, online: bool) -> None:
        with self.lock:
            self.offline = not online
            if online:
                self._say("Интернет снова есть — продолжаю с того же места…")
            else:
                self._say("Нет интернета. Жду подключения — работа продолжится сама, ничего нажимать не нужно.", "warn")

    def on_download(self, key: str, status: str, percent: int) -> None:
        with self.lock:
            i = self.index.get(key)
            if i is None:
                return
            row = self.rows[i]
            if status == "Retry":  # the job tries a failed download again
                if row["s"] == "dlerror":
                    self.phase_done = max(0, self.phase_done - 1)
                    self._count("missing", -1)
                self._set(i, s="retrying", p=0)
                return
            if row["s"] in DOWNLOAD_FINAL:
                return
            if status in ("Done", "Error", "Skipped"):
                self.phase_done += 1
                if status == "Error":
                    self._count("missing")
                    self._set(i, s="dlerror", p=0)
                else:
                    self._count("done")
                    self._set(i, s="done" if status == "Done" else "exists", p=100)
            else:
                state = STATE_BY_DOWNLOAD_MESSAGE.get(status, "downloading")
                percent = max(0, min(100, int(percent or 0)))
                if state == row["s"] and percent == row["p"]:
                    return
                self._set(i, s=state, p=percent)

    def on_done(self, summary: Any) -> None:
        with self.lock:
            failed = len(summary.failed)
            if self.dry_run:
                text = f"Проверка закончена: найдено {summary.succeeded} из {summary.processed}."
            else:
                text = f"Готово: скачано {summary.succeeded} из {summary.processed}."
            tone = "ok"
            if summary.cancelled:
                text = f"Остановлено. Обработано {summary.processed - summary.cancelled} из {summary.processed}."
                tone = "warn"
            if summary.existing:
                text += f" Уже были скачаны раньше: {summary.existing}."
            if failed:
                text += f" Не получилось: {failed} — см. «Список ненайденных»."
                tone = "warn" if tone == "ok" else tone
            self.report = str(summary.report) if summary.report else None
            self.summary = {
                "dryRun": self.dry_run,
                "total": summary.total,
                "existing": summary.existing,
                "processed": summary.processed,
                "succeeded": summary.succeeded,
                "failed": failed,
                "cancelled": summary.cancelled,
                "allFailed": bool(failed and summary.processed and failed == summary.processed),
            }
            self.running, self.can_stop, self.offline = False, False, False
            self.phase, self.finished = "done", time.time()
            self.phase_done = self.phase_total = max(1, self.phase_total)
            self._say(text, tone)
        self.flush()

    def on_error(self, message: str, details: Optional[str]) -> None:
        with self.lock:
            self.running, self.can_stop, self.offline = False, False, False
            self.phase, self.finished = "done", time.time()
            self.error = {"message": message, "logged": bool(details)}
            self._say(f"Ошибка: {message}", "bad")
        self.flush()

    # ------------------------------------------------------------------ helpers
    def link_for(self, row: int) -> Optional[str]:
        with self.lock:
            if 0 <= row < len(self.links) and self.links[row]:
                return self.links[row]
        return None

    def shutdown(self) -> None:
        """The window is gone: stop a running job."""
        with self.lock:
            if self.cancel is not None:
                self.cancel.set()
