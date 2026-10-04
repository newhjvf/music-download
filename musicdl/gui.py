"""Window with buttons (tkinter, ships with Python on Windows).

Start: double-click ``start.bat`` or run ``musicdl-gui``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path.home() / ".musicdl"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "log.txt"
DEFAULT_OUT = Path.home() / "Music" / "musicdl"

BITRATES = ["128k", "192k", "256k", "320k"]

# spotDL progress messages -> what the user sees
DOWNLOAD_STATUS = {
    "Searching": "поиск…",
    "Getting metadata": "подготовка…",
    "Downloading": "загрузка",
    "Converting": "конвертация в mp3…",
    "Embedding metadata": "запись тегов…",
    "Done": "✔ скачано",
    "Skipped": "✔ уже есть",
    "Error": "✖ ошибка загрузки",
}


def human_status(message: str, percent: int) -> str:
    text = DOWNLOAD_STATUS.get(message, message or "загрузка")
    if text == "загрузка" and 0 < percent < 100:
        text = f"загрузка {percent}%"
    return text


def source_label(match) -> str:
    """'YouTube Music', 'YouTube Music, видео', 'SoundCloud'..."""
    source = match.source or "YouTube Music"
    if source == "YouTube Music" and not match.verified:
        return "YouTube Music, видео"
    return source


def load_settings() -> Dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_path(path: Path) -> None:
    """Open a folder/file with the system's default program."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # pylint: disable=no-member
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])



PALETTE = {
    "dark": {"ok": "#6ccb5f", "bad": "#ff6b6b", "active": "#60cdff", "muted": "#9a9a9a", "accent": "#60cdff"},
    "light": {"ok": "#0f7b0f", "bad": "#c42b1c", "active": "#005fb8", "muted": "#6e6e6e", "accent": "#005fb8"},
}

FINAL_STATUSES = ("✔ скачано", "✖ ошибка загрузки", "✔ уже есть")


def apply_theme(root: tk.Misc, theme: str) -> str:
    """Windows 11 look (sv-ttk) if available, otherwise the best built-in theme."""
    try:
        import sv_ttk

        sv_ttk.set_theme(theme)
        return theme
    except Exception:
        try:
            ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        return "light"


def ui_font(size: int, weight: str = "normal") -> tuple:
    family = "Segoe UI Variable Display" if sys.platform == "win32" else "TkDefaultFont"
    return (family, size, weight)


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


class App:
    """The main window. Work runs in a background thread; it sends events
    through ``self.events`` and the UI applies them in ``_poll``."""

    def __init__(self, root: tk.Tk, run_job: Optional[Callable] = None) -> None:
        from musicdl.job import run_job as default_run_job

        self.root = root
        self.run_job = run_job or default_run_job
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel: Optional[threading.Event] = None
        self.rows: Dict[str, str] = {}  # song.url -> tree item
        self.links: Dict[str, str] = {}  # tree item -> found URL
        self.report: Optional[Path] = None
        self.searched = self.search_total = 0
        self.phase_name = ""
        self.phase_done = self.phase_total = 0
        self.phase_started = 0.0
        self.started = 0.0
        self.offline = False

        settings = load_settings()
        self.theme = settings.get("theme", "dark")
        self.mode = tk.StringVar(value=settings.get("mode", "csv"))
        self.csv_path = tk.StringVar(value=settings.get("csv_path", ""))
        self.link = tk.StringVar(value=settings.get("link", ""))
        self.query_text = str(settings.get("query", ""))
        self.last_status_update = 0.0
        self.out_dir = tk.StringVar(value=settings.get("out_dir", str(DEFAULT_OUT)))
        self.bitrate = tk.StringVar(value=settings.get("bitrate", "320k"))
        self.threads = tk.IntVar(value=int(settings.get("threads", 3)))
        self.only_verified = tk.BooleanVar(value=bool(settings.get("only_verified", False)))
        self.status = tk.StringVar(value="Выберите CSV-файл, вставьте ссылку Spotify или введите названия треков, затем нажмите «Проверить» или «Скачать».")
        self.timing = tk.StringVar(value="")
        self.percent = tk.StringVar(value="")
        self.counters = {name: tk.StringVar(value="—") for name in ("total", "existing", "found", "missing", "done")}

        from musicdl.updater import version_label

        self.version = version_label()
        root.title(f"musicdl {self.version} — скачивание музыки")
        root.minsize(900, 640)
        root.geometry("1120x780")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.theme = apply_theme(root, self.theme)
        self._build()
        self._apply_tags()
        self._on_mode_change()
        self.root.after(100, self._poll)
        self.root.after(100, self._tick)

    # ----------------------------------------------------------------- layout
    def _card(self, parent, title: str) -> "tuple[ttk.Frame, ttk.Frame]":
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        ttk.Label(outer, text=title, font=ui_font(11, "bold")).pack(anchor="w", pady=(0, 6))
        inner = ttk.Frame(outer)
        inner.pack(fill="x")
        return outer, inner

    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=(18, 12))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="musicdl", font=ui_font(22, "bold")).pack(side="left")
        ttk.Label(header, text=f"   скачивание музыки по спискам · {self.version}", foreground=self._color("muted")).pack(
            side="left", pady=(8, 0)
        )
        self.theme_button = ttk.Button(header, text=self._theme_icon(), width=3, command=self.toggle_theme)
        self.theme_button.pack(side="right")

        top = ttk.Frame(main)
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)

        source_card, source = self._card(top, "Откуда брать треки")
        source_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        source.columnconfigure(1, weight=1)
        ttk.Radiobutton(source, text="CSV", value="csv", variable=self.mode, command=self._on_mode_change).grid(
            row=0, column=0, sticky="w"
        )
        self.csv_entry = ttk.Entry(source, textvariable=self.csv_path)
        self.csv_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.csv_button = ttk.Button(source, text="Выбрать файл…", command=self.choose_csv)
        self.csv_button.grid(row=0, column=2, sticky="ew")
        ttk.Radiobutton(source, text="Ссылка Spotify", value="url", variable=self.mode, command=self._on_mode_change).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.link_entry = ttk.Entry(source, textvariable=self.link)
        self.link_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.paste_button = ttk.Button(source, text="Вставить", command=self.paste_link)
        self.paste_button.grid(row=1, column=2, sticky="ew", pady=(8, 0))
        ttk.Radiobutton(source, text="Названия треков", value="text", variable=self.mode, command=self._on_mode_change).grid(
            row=2, column=0, sticky="nw", pady=(8, 0)
        )
        self.query_box = tk.Text(source, height=3, wrap="word", relief="flat", borderwidth=0, highlightthickness=1, undo=True)
        self.query_box.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.query_box.insert("1.0", self.query_text)
        ttk.Label(source, text="по одному треку в строке: «Артист - Название»", foreground=self._color("muted")).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=8
        )

        target_card, target = self._card(top, "Куда и как сохранять")
        target_card.grid(row=0, column=1, sticky="nsew")
        target.columnconfigure(0, weight=1)
        ttk.Entry(target, textvariable=self.out_dir).grid(row=0, column=0, columnspan=3, sticky="ew", padx=(0, 8))
        ttk.Button(target, text="Папка…", command=self.choose_out_dir).grid(row=0, column=3)
        options = ttk.Frame(target)
        options.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(options, text="Качество").pack(side="left")
        ttk.Combobox(options, textvariable=self.bitrate, values=BITRATES, width=6, state="readonly").pack(side="left", padx=(6, 14))
        ttk.Label(options, text="Одновременно").pack(side="left")
        ttk.Spinbox(options, from_=1, to=8, textvariable=self.threads, width=4).pack(side="left", padx=6)
        verified_box = ttk.Frame(target)
        verified_box.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        try:
            ttk.Checkbutton(
                verified_box, text="Только официальные треки", variable=self.only_verified, style="Switch.TCheckbutton"
            ).pack(side="left")
        except tk.TclError:
            ttk.Checkbutton(verified_box, text="Только официальные треки", variable=self.only_verified).pack(side="left")

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, sticky="ew", pady=12)
        self.download_button = ttk.Button(actions, text="⬇  Скачать", command=lambda: self.start(False), style="Accent.TButton")
        self.download_button.pack(side="left", ipadx=14, ipady=4)
        self.check_button = ttk.Button(actions, text="🔍  Проверить без скачивания", command=lambda: self.start(True))
        self.check_button.pack(side="left", padx=8, ipady=4)
        self.stop_button = ttk.Button(actions, text="■  Стоп", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", ipady=4)
        ttk.Label(actions, textvariable=self.timing, foreground=self._color("muted")).pack(side="right")

        tiles = ttk.Frame(main)
        tiles.grid(row=3, column=0, sticky="ew")
        self.tile_labels = {}
        for index, (name, caption, color) in enumerate(
            (
                ("total", "в списке", None),
                ("existing", "уже были", "muted"),
                ("found", "найдено", "active"),
                ("missing", "не найдено", "bad"),
                ("done", "скачано", "ok"),
            )
        ):
            tiles.columnconfigure(index, weight=1)
            tile = ttk.Frame(tiles, style="Card.TFrame", padding=(14, 8))
            tile.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            number = ttk.Label(tile, textvariable=self.counters[name], font=ui_font(22, "bold"))
            number.pack(anchor="w")
            self.tile_labels[name] = (number, color)
            ttk.Label(tile, text=caption, foreground=self._color("muted")).pack(anchor="w")

        progress = ttk.Frame(main)
        progress.grid(row=4, column=0, sticky="ew", pady=(12, 2))
        progress.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress, textvariable=self.percent, width=6, anchor="e").grid(row=0, column=1)
        self.status_label = ttk.Label(main, textvariable=self.status, wraplength=1000)
        self.status_label.grid(row=5, column=0, sticky="w", pady=(2, 8))

        table_frame = ttk.Frame(main)
        table_frame.grid(row=6, column=0, sticky="nsew")
        main.rowconfigure(6, weight=1)
        columns = ("n", "track", "found", "source", "duration", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        for column, title, width, stretch in (
            ("n", "№", 44, False),
            ("track", "Трек", 280, True),
            ("found", "Найдено", 280, True),
            ("source", "Источник", 130, False),
            ("duration", "Длит.", 60, False),
            ("status", "Статус", 190, False),
        ):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, stretch=stretch, anchor="e" if column in ("n", "duration") else "w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self._open_row_link)

        bottom = ttk.Frame(main)
        bottom.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(bottom, text="📂  Папка с музыкой", command=self.open_out_dir).pack(side="left")
        self.report_button = ttk.Button(bottom, text="Список ненайденных", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=8)
        ttk.Label(bottom, text="Двойной щелчок по строке — открыть найденное в браузере", foreground=self._color("muted")).pack(
            side="left", padx=8
        )
        ttk.Button(bottom, text="Журнал ошибок", command=lambda: self._open_if_exists(LOG_FILE)).pack(side="right")

    def _color(self, name: str) -> str:
        return PALETTE.get(self.theme, PALETTE["light"])[name]

    def _theme_icon(self) -> str:
        return "☀" if self.theme == "dark" else "🌙"

    def _style_query_box(self) -> None:
        style = ttk.Style()
        background = style.lookup("TEntry", "fieldbackground") or style.lookup("TEntry", "background") or "white"
        foreground = style.lookup("TEntry", "foreground") or "black"
        self.query_box.configure(
            background=background,
            foreground=foreground,
            insertbackground=foreground,
            highlightbackground=self._color("muted"),
            highlightcolor=self._color("accent"),
            font=ui_font(10),
        )

    def _apply_tags(self) -> None:
        self._style_query_box()
        for tag in ("ok", "bad", "active", "muted"):
            self.table.tag_configure(tag, foreground=self._color(tag))
        for number, color in self.tile_labels.values():
            number.configure(foreground=self._color(color) if color else "")

    def toggle_theme(self) -> None:
        self.theme = apply_theme(self.root, "light" if self.theme == "dark" else "dark")
        self.theme_button.configure(text=self._theme_icon())
        self._apply_tags()
        self._save()

    def _on_mode_change(self) -> None:
        mode = self.mode.get()
        self.csv_entry.configure(state="normal" if mode == "csv" else "disabled")
        self.csv_button.configure(state="normal" if mode == "csv" else "disabled")
        self.link_entry.configure(state="normal" if mode == "url" else "disabled")
        self.paste_button.configure(state="normal" if mode == "url" else "disabled")
        self.query_box.configure(state="normal" if mode == "text" else "disabled")

    # ---------------------------------------------------------------- actions
    def choose_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="CSV-файл из TuneMyMusic", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if path:
            self.csv_path.set(path)

    def paste_link(self) -> None:
        try:
            self.link.set(self.root.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo("musicdl", "В буфере обмена нет текста. Скопируйте ссылку в Spotify: «Поделиться → Копировать ссылку».")

    def choose_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Куда сохранять музыку", initialdir=self.out_dir.get() or str(Path.home()))
        if path:
            self.out_dir.set(path)

    def open_out_dir(self) -> None:
        out = Path(self.out_dir.get())
        out.mkdir(parents=True, exist_ok=True)
        open_path(out)

    def open_report(self) -> None:
        if self.report:
            self._open_if_exists(self.report)

    def _open_if_exists(self, path: Path) -> None:
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo("musicdl", f"Файла пока нет:\n{path}")

    def _open_row_link(self, _event) -> None:
        link = self.links.get(self.table.focus())
        if link:
            import webbrowser

            webbrowser.open(link)

    def _query(self) -> str:
        return self.query_box.get("1.0", "end").strip()

    def _validate(self) -> Optional[str]:
        if self.mode.get() == "csv":
            path = self.csv_path.get().strip()
            if not path:
                return "Выберите CSV-файл (кнопка «Выбрать файл…»)."
            if not Path(path).is_file():
                return f"Файл не найден:\n{path}"
        elif self.mode.get() == "text":
            if not self._query():
                return "Введите названия треков: по одному в строке, например «Aarne - CULTURE»."
        else:
            from musicdl.spotify_input import SpotifyInputError, check_url

            try:
                check_url(self.link.get())
            except SpotifyInputError:
                return (
                    "Это не похоже на ссылку Spotify на трек, альбом или плейлист.\n"
                    "Скопируйте её в Spotify: «Поделиться → Копировать ссылку»."
                )
        if not self.out_dir.get().strip():
            return "Выберите папку для музыки."
        try:
            if not 1 <= int(self.threads.get()) <= 8:
                raise ValueError
        except (tk.TclError, ValueError):
            return "«Одновременно» — число от 1 до 8."
        return None

    def options(self, dry_run: bool):
        from musicdl.job import JobOptions

        mode = self.mode.get()
        return JobOptions(
            source_kind=mode,
            source={"csv": self.csv_path.get, "url": self.link.get, "text": self._query}[mode]().strip(),
            out_dir=Path(self.out_dir.get().strip()),
            threads=int(self.threads.get()),
            bitrate=self.bitrate.get(),
            dry_run=dry_run,
            only_verified=bool(self.only_verified.get()),
            env_file=Path.cwd() / ".env",
            cancel=self.cancel,
        )

    def start(self, dry_run: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        problem = self._validate()
        if problem:
            messagebox.showwarning("musicdl", problem)
            return
        self._save()
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        self.links.clear()
        self.report = None
        self.report_button.configure(state="disabled")
        for var in self.counters.values():
            var.set("0")
        self.counters["total"].set("…")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.percent.set("")
        self.timing.set("")
        self.phase_name = ""
        self.started = time.monotonic()
        self.cancel = threading.Event()
        self.offline = False
        self.status_label.configure(foreground="")
        self._set_busy(True)
        self.status.set("Загружаю список треков…")

        options = self.options(dry_run)
        self.worker = threading.Thread(target=self._work, args=(options,), daemon=True)
        self.worker.start()

    def stop(self) -> None:
        if self.cancel is not None and not self.cancel.is_set():
            self.cancel.set()
            self.stop_button.configure(state="disabled")
            self.status.set("Останавливаю: дожидаюсь треков, которые уже ищутся…")

    def _work(self, options) -> None:
        from musicdl.job import JobError, JobEvents

        post = self.events.put
        events = JobEvents(
            info=lambda message: post(("info", message)),
            songs_loaded=lambda todo, existing: post(("songs", todo, existing)),
            matched=lambda match: post(("match", match, options.dry_run)),
            download_status=lambda key, status, percent: post(("download", key, status, percent)),
            phase=lambda name, count: post(("phase", name, count)),
            searching=lambda key, source: post(("searching", key, source)),
            connection=lambda online: post(("connection", online)),
        )
        try:
            summary = self.run_job(options, events)
        except JobError as exc:
            post(("error", str(exc), None))
        except Exception as exc:  # unexpected: show message, keep details in the log
            logging.getLogger("musicdl").error("Unexpected error", exc_info=True)
            post(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
        else:
            post(("done", summary, options.dry_run))

    # ------------------------------------------------------------ UI updates
    def _poll(self) -> None:
        """Apply queued events, but never for longer than a moment: a burst of
        events must not keep the window from redrawing and reacting."""
        deadline = time.monotonic() + 0.03
        try:
            while time.monotonic() < deadline:
                event = self.events.get_nowait()
                getattr(self, f"_on_{event[0]}")(*event[1:])
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _tick(self) -> None:
        """Elapsed / remaining time (once a second, no row animations)."""
        if self.worker and self.worker.is_alive() and self.started:
            elapsed = time.monotonic() - self.started
            text = f"прошло {format_seconds(elapsed)}"
            if self.phase_total and self.phase_done:
                per_item = (time.monotonic() - self.phase_started) / self.phase_done
                remaining = per_item * (self.phase_total - self.phase_done)
                text += f" · осталось ≈ {format_seconds(remaining)}"
            self.timing.set(text)
        self.root.after(1000, self._tick)

    def _advance(self) -> None:
        self.phase_done += 1
        self.progress.configure(value=self.phase_done)
        if self.phase_total:
            self.percent.set(f"{min(100, round(100 * self.phase_done / self.phase_total))}%")

    def _bump(self, name: str) -> None:
        var = self.counters[name]
        try:
            var.set(str(int(var.get()) + 1))
        except ValueError:
            var.set("1")

    def _on_info(self, message: str) -> None:
        if not self.offline:
            self.status.set(message)

    def _on_songs(self, todo: List, existing: List) -> None:
        from musicdl.pipeline import expected_path

        number = 0
        for song in existing:
            number += 1
            item = self.table.insert(
                "",
                "end",
                values=(number, f"{song.artist} - {song.name}", expected_path(song, Path(self.out_dir.get())).name, "", "", "✔ уже есть"),
                tags=("muted",),
            )
            self.rows[song.url] = item
        for song in todo:
            number += 1
            self.rows[song.url] = self.table.insert(
                "", "end", values=(number, f"{song.artist} - {song.name}", "", "", "", "· в очереди")
            )
        self.counters["total"].set(str(len(todo) + len(existing)))
        self.counters["existing"].set(str(len(existing)))

    def _on_phase(self, name: str, count: int) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0, maximum=max(1, count))
        self.percent.set("0%")
        self.phase_name, self.phase_total, self.phase_done = name, count, 0
        self.phase_started = time.monotonic()
        if name == "search":
            self.searched, self.search_total = 0, count
        else:
            self.stop_button.configure(state="disabled")

    def _on_searching(self, key: str, source: str) -> None:
        item = self.rows.get(key)
        if item is not None:
            self.table.item(item, tags=("active",))
            self.table.set(item, "status", f"🔍 ищу · {source}")
            now = time.monotonic()
            if not self.offline and now - self.last_status_update > 0.4:
                self.last_status_update = now
                track = self.table.set(item, "track")
                self.status.set(f"Ищу трек {self.searched + 1} из {self.search_total}: «{track}» на {source}…")

    def _on_match(self, match, dry_run: bool) -> None:
        from musicdl.matching import CANCELLED
        from musicdl.pipeline import format_duration

        self.searched += 1
        self._advance()
        item = self.rows.get(match.song.url)
        if item is None:
            return
        values = (self.table.set(item, "n"), self.table.set(item, "track"))
        if match.found:
            self._bump("found")
            found = f"{match.author} - {match.title}" if match.title else match.url
            self.links[item] = match.url
            status = "✔ найдено" if dry_run else "· ждёт загрузки"
            self.table.item(
                item,
                values=values + (found, source_label(match), format_duration(match.duration), status),
                tags=("ok",) if dry_run else (),
            )
        elif match.error == CANCELLED:
            self.table.item(item, values=values + ("", "", "", "⏹ остановлено"), tags=("muted",))
        else:
            self._bump("missing")
            not_found = match.error == "not found"
            self.table.item(
                item,
                values=values + ("—" if not_found else (match.error or ""), "", "", "✖ не найдено" if not_found else "✖ ошибка поиска"),
                tags=("bad",),
            )

    def _on_connection(self, online: bool) -> None:
        if online:
            self.offline = False
            self.status_label.configure(foreground="")
            self.status.set("Интернет снова есть — продолжаю с того же места…")
        else:
            self.offline = True
            self.status_label.configure(foreground=self._color("bad"))
            self.status.set("⚠ Нет интернета. Жду подключения — работа продолжится сама, ничего нажимать не нужно.")

    def _on_download(self, key: str, status: str, percent: int) -> None:
        item = self.rows.get(key)
        if item is None:
            return
        current = self.table.set(item, "status")
        if status == "Retry":  # the job tries a failed download again
            if current == "✖ ошибка загрузки":
                self.phase_done = max(0, self.phase_done - 1)
                self.progress.configure(value=self.phase_done)
                count = self.counters["missing"]
                count.set(str(max(0, int(count.get() or 0) - 1)))
            self.table.set(item, "status", "⏳ повторная загрузка…")
            self.table.item(item, tags=("active",))
            return
        if current in FINAL_STATUSES:
            return
        if status in ("Done", "Error", "Skipped"):
            self.table.set(item, "status", human_status(status, percent))
            self.table.item(item, tags=("bad",) if status == "Error" else ("ok",))
            self._advance()
            self._bump("missing" if status == "Error" else "done")
        else:
            self.table.set(item, "status", f"⏳ {human_status(status, 0)}")
            self.table.item(item, tags=("active",))

    def _on_done(self, summary, dry_run: bool) -> None:
        self._set_busy(False)
        self.offline = False
        self.status_label.configure(foreground="")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=1, value=1)
        self.percent.set("100%")
        elapsed = format_seconds(time.monotonic() - self.started) if self.started else ""
        self.timing.set(f"готово за {elapsed}" if elapsed else "")
        failed = len(summary.failed)
        if dry_run:
            text = f"Проверка закончена: найдено {summary.succeeded} из {summary.processed}."
        else:
            text = f"Готово: скачано {summary.succeeded} из {summary.processed}."
        if summary.cancelled:
            text = f"Остановлено. Обработано {summary.processed - summary.cancelled} из {summary.processed}."
        if summary.existing:
            text += f" Уже были скачаны раньше: {summary.existing}."
        if failed:
            text += f" Не получилось: {failed} — см. «Список ненайденных»."
        self.status.set(text)
        self.report = summary.report
        self.report_button.configure(state="normal" if summary.report else "disabled")
        if failed and summary.processed and failed == summary.processed:
            messagebox.showwarning(
                "musicdl",
                "Не удалось найти или скачать ни одного трека.\n\n"
                "Чаще всего это значит, что YouTube временно ограничил доступ. "
                "Попробуйте позже, включите VPN или уменьшите «Одновременно» до 1–2.",
            )

    def _on_error(self, message: str, details: Optional[str]) -> None:
        self._set_busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status.set(f"Ошибка: {message}")
        extra = f"\n\nПодробности записаны в журнал:\n{LOG_FILE}" if details else ""
        messagebox.showerror("musicdl", message + extra)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.check_button.configure(state=state)
        self.download_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _save(self) -> None:
        save_settings(
            {
                "mode": self.mode.get(),
                "csv_path": self.csv_path.get(),
                "link": self.link.get(),
                "query": self._query(),
                "out_dir": self.out_dir.get(),
                "bitrate": self.bitrate.get(),
                "threads": int(self.threads.get()),
                "only_verified": bool(self.only_verified.get()),
                "theme": self.theme,
            }
        )

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("musicdl", "Идёт работа. Закрыть программу? Недокачанные треки можно докачать потом."):
                return
        self._save()
        self.root.destroy()


def _setup_io() -> None:
    """pythonw.exe has no console: send output and logs to ~/.musicdl/log.txt."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    if sys.stdout is None or sys.stderr is None or "pythonw" in Path(sys.executable).name.lower():
        sys.stdout = log
        sys.stderr = log
    from musicdl.winproc import hide_child_consoles

    hide_child_consoles()  # no black ffmpeg windows
    logging.basicConfig(
        stream=log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("spotdl", "yt_dlp", "urllib3", "ytmusicapi"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _enable_hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # older Windows
            pass


def run_update_check(root: tk.Tk) -> bool:
    """Small "checking for updates" screen. Returns True if the program was
    updated and a fresh copy has been started (this one should exit)."""
    from musicdl import updater

    if updater.install_root() is None:
        return False

    message = tk.StringVar(value="Проверяю обновления…")
    frame = ttk.Frame(root, padding=30)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, textvariable=message).pack()
    bar = ttk.Progressbar(frame, mode="indeterminate", length=260)
    bar.pack(pady=10)
    bar.start(15)

    result: Dict[str, bool] = {}
    # Tk may only be touched from the main thread (Python 3.14 on Windows
    # raises "main thread is not in main loop" otherwise): the worker only
    # puts texts into a queue, the loop below shows them.
    texts: "queue.Queue[str]" = queue.Queue()

    def work() -> None:
        try:
            result["updated"] = updater.check_and_update(texts.put)
        except Exception:  # never block the program because of the updater
            logging.getLogger("musicdl").exception("Updater crashed")
            result["updated"] = False

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    while thread.is_alive() or not texts.empty():
        try:
            while True:
                message.set(texts.get_nowait())
        except queue.Empty:
            pass
        root.update()
        thread.join(0.05)
    bar.stop()
    frame.destroy()

    if result.get("updated"):
        subprocess.Popen([sys.executable, "-m", "musicdl.gui", "--updated"], cwd=str(updater.install_root()))
        return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _setup_io()
    _enable_hidpi()
    root = tk.Tk()
    root.title("musicdl")
    apply_theme(root, load_settings().get("theme", "dark"))
    if "--updated" not in argv and "--no-update" not in argv and run_update_check(root):
        root.destroy()
        return 0
    app = App(root)
    if "--updated" in argv:
        app.status.set("Программа обновлена до последней версии. " + app.status.get())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
