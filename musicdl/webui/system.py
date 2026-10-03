"""Small operating-system helpers: open files, pick files/folders, find a browser."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_path(path: Path) -> None:
    """Open a folder/file with the system's default program."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # pylint: disable=no-member
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_url(url: str) -> None:
    webbrowser.open(url)


def pick_path(kind: str, initial: str = "") -> Optional[str]:
    """Native "open file" / "choose folder" dialog (tkinter, ships with Python
    on Windows) in a helper process, so the web server never touches Tk.
    Returns None if cancelled; raises RuntimeError if dialogs are unavailable."""
    handle, result_name = tempfile.mkstemp(prefix="musicdl-pick-", suffix=".txt")
    os.close(handle)
    try:
        process = subprocess.run(
            [sys.executable, "-m", "musicdl.webui.pick", kind, initial, result_name],
            capture_output=True,
            creationflags=NO_WINDOW,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError("Окно выбора файла недоступно")
        return Path(result_name).read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        raise RuntimeError("Окно выбора файла недоступно") from exc
    finally:
        try:
            os.unlink(result_name)
        except OSError:
            pass


def _browser_candidates() -> List[Path]:
    found: List[Path] = []
    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
        for root in filter(None, roots):
            found.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        for root in filter(None, roots):
            found.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        found += [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    for name in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            found.append(Path(path))
    return found


def find_app_browser() -> Optional[Path]:
    """Edge (always there on Windows 10/11) or Chrome: both can show a page as
    a plain app window without tabs and address bar."""
    for candidate in _browser_candidates():
        if candidate.is_file():
            return candidate
    return None


def open_app_window(url: str, profile_dir: Path, browser: Optional[Path] = None) -> bool:
    """Show ``url`` in its own chromeless window. Falls back to the default
    browser (a normal tab). Returns True if a dedicated window was opened."""
    exe = browser or find_app_browser()
    if exe is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(exe),
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1240,860",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        if sys.platform != "win32" and os.geteuid() == 0:  # e.g. inside a container
            args.append("--no-sandbox")
        try:
            subprocess.Popen(args)
            return True
        except OSError:
            logger.warning("Could not start %s", exe, exc_info=True)
    webbrowser.open(url)
    return False
