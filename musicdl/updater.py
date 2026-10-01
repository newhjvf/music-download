"""Self-update from GitHub (branch ``main``).

On start the window asks GitHub for the latest commit of ``main``; if it
differs from the installed one, the source ZIP is downloaded and the
program files are replaced in place (``.venv``, ``.env`` and the user's
music are never touched). If ``pyproject.toml`` changed, dependencies are
reinstalled with pip. Only standard library is used, so a broken update of
the package itself cannot break the updater's download step.

Development checkouts (a ``.git`` folder next to ``pyproject.toml``) are
never updated.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

REPO = "newhjvf/music-download"
BRANCH = "main"
API_URL = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
ZIP_URL = "https://codeload.github.com/{repo}/zip/{sha}"
STATE_NAME = ".musicdl-version.json"

# Everything the program consists of; anything else in the folder is left alone.
MANAGED = (
    "musicdl",
    "examples",
    "tests",
    "README.md",
    "pyproject.toml",
    "install.bat",
    "start.bat",
    ".env.example",
    ".gitignore",
    ".gitattributes",
)

Fetch = Callable[[str, float], bytes]


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "musicdl-updater"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return response.read()


def install_root(start: Optional[Path] = None) -> Optional[Path]:
    """Folder the program was installed into, or None if it should not be
    self-updated (git checkout or non-editable install)."""
    root = (start or Path(__file__)).resolve().parent.parent
    if not (root / "pyproject.toml").is_file() or (root / ".git").exists():
        return None
    return root


def read_state(root: Path) -> Dict:
    try:
        return json.loads((root / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(root: Path, state: Dict) -> None:
    (root / STATE_NAME).write_text(json.dumps(state, indent=2), encoding="utf-8")


def latest_commit(fetch: Fetch = _fetch, timeout: float = 5) -> Tuple[str, str]:
    """-> (sha, ISO date of the commit)."""
    data = json.loads(fetch(API_URL, timeout).decode("utf-8"))
    date = (data.get("commit") or {}).get("committer", {}).get("date", "")
    return data["sha"], date


def latest_sha(fetch: Fetch = _fetch, timeout: float = 5) -> str:
    return latest_commit(fetch, timeout)[0]


def _replace_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".new")
    shutil.copy2(source, staged)
    os.replace(staged, target)


def _sync_dir(source: Path, target: Path) -> None:
    """Make ``target`` contain exactly the files of ``source`` (``__pycache__``
    ignored). Files are replaced one by one: on Windows this works even when
    the folder itself is in use, unlike renaming the whole folder."""
    wanted = set()
    for file in source.rglob("*"):
        if file.is_file() and "__pycache__" not in file.parts:
            relative = file.relative_to(source)
            wanted.add(relative)
            _replace_file(file, target / relative)
    if target.exists():
        for file in sorted(target.rglob("*"), reverse=True):
            relative = file.relative_to(target)
            if "__pycache__" in relative.parts:
                continue
            try:
                if file.is_file() and relative not in wanted:
                    file.unlink()
                elif file.is_dir() and not any(file.iterdir()):
                    file.rmdir()
            except OSError:
                logger.debug("Could not remove %s", file)


def apply_update(zip_bytes: bytes, root: Path) -> bool:
    """Replace program files in ``root`` with the archive contents.
    Returns True if ``pyproject.toml`` changed (dependencies may need pip)."""
    old_pyproject = (root / "pyproject.toml").read_bytes() if (root / "pyproject.toml").exists() else b""

    with tempfile.TemporaryDirectory(dir=root, prefix=".update-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            archive.extractall(tmp_path)
        tops = [p for p in tmp_path.iterdir() if p.is_dir()]
        if len(tops) != 1 or not (tops[0] / "musicdl" / "__init__.py").is_file():
            raise ValueError("unexpected archive layout")
        source = tops[0]

        for name in MANAGED:
            new = source / name
            if not new.exists():
                continue
            if new.is_dir():
                _sync_dir(new, root / name)
            else:
                _replace_file(new, root / name)

    return (root / "pyproject.toml").read_bytes() != old_pyproject


def version_label(root: Optional[Path] = None) -> str:
    """'версия от 01.10.2026 14:05' for the window title."""
    root = root or install_root()
    if root is None:
        return "версия для разработки"
    state = read_state(root)
    date = state.get("date", "")
    if len(date) >= 16:
        return f"версия от {date[8:10]}.{date[5:7]}.{date[0:4]} {date[11:16]} UTC"
    return "версия не определена"


def console_python() -> str:
    """python.exe next to pythonw.exe (pip needs a console interpreter)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return sys.executable


def install_dependencies(root: Path) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [console_python(), "-m", "pip", "install", "--disable-pip-version-check", "-e", str(root)],
        capture_output=True,
        text=True,
        timeout=1800,
        creationflags=flags,
    )
    if result.returncode != 0:
        logger.error("pip install failed:\n%s\n%s", result.stdout[-4000:], result.stderr[-4000:])
    return result.returncode == 0


def check_and_update(
    status: Callable[[str], None] = lambda message: None,
    fetch: Fetch = _fetch,
    root: Optional[Path] = None,
    pip: Callable[[Path], bool] = install_dependencies,
) -> bool:
    """Returns True if new files were installed (the program should restart).
    Network problems are logged and ignored: the program then simply starts."""
    root = root or install_root()
    if root is None:
        return False
    state = read_state(root)
    try:
        sha, date = latest_commit(fetch)
    except Exception as exc:  # offline, GitHub rate limit, ...
        logger.info("Update check skipped: %s", exc)
        return False

    if sha == state.get("sha"):
        if date and state.get("date") != date:
            state["date"] = date
            write_state(root, state)
        if state.get("pip_failed"):  # retry a failed dependency install
            status("Доустанавливаю компоненты…")
            state["pip_failed"] = not pip(root)
            write_state(root, state)
        return False

    status("Скачиваю обновление…")
    try:
        deps_changed = apply_update(fetch(ZIP_URL.format(repo=REPO, sha=sha), 60), root)
    except Exception:
        logger.exception("Update failed")
        return False

    state = {"sha": sha, "date": date, "pip_failed": False}
    if deps_changed:
        status("Устанавливаю новые компоненты (может занять пару минут)…")
        state["pip_failed"] = not pip(root)
    write_state(root, state)
    logger.info("Updated to %s", sha)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("updated" if check_and_update(print) else "up to date")
