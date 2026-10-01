import io
import json
import zipfile
from pathlib import Path

from musicdl import updater

SHA1 = "a" * 40
SHA2 = "b" * 40


def make_zip(sha, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(f"music-download-{sha}/{name}", content)
    return buffer.getvalue()


def release(version, pyproject="deps-1"):
    return {
        "musicdl/__init__.py": f"__version__ = '{version}'\n",
        "musicdl/gui.py": f"# gui {version}\n",
        "pyproject.toml": pyproject,
        "README.md": f"readme {version}",
        "start.bat": "@echo off\r\n",
    }


class FakeGitHub:
    def __init__(self, sha, files):
        self.sha, self.files, self.offline, self.calls = sha, files, False, []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.offline:
            raise OSError("no internet")
        if url == updater.API_URL:
            return json.dumps({"sha": self.sha, "commit": {"committer": {"date": "2026-10-01T11:05:59Z"}}}).encode()
        assert url.endswith(self.sha)
        return make_zip(self.sha, self.files)


def installed(tmp_path):
    root = tmp_path / "music-download-main"
    (root / "musicdl").mkdir(parents=True)
    (root / "musicdl" / "__init__.py").write_text("__version__ = 'old'\n")
    (root / "musicdl" / "removed_module.py").write_text("old")
    (root / "pyproject.toml").write_text("deps-1")
    (root / ".venv").mkdir()
    (root / ".venv" / "keep.txt").write_text("venv")
    (root / ".env").write_text("SPOTIFY_CLIENT_ID=secret")
    return root


def test_install_root(tmp_path):
    root = installed(tmp_path)
    module = root / "musicdl" / "updater.py"
    assert updater.install_root(module) == root
    (root / ".git").mkdir()
    assert updater.install_root(module) is None  # dev checkout is never updated


def test_update_replaces_program_keeps_user_files(tmp_path):
    root = installed(tmp_path)
    github = FakeGitHub(SHA1, release("1"))
    pip_calls = []
    assert updater.check_and_update(fetch=github, root=root, pip=pip_calls.append) is True

    assert (root / "musicdl" / "__init__.py").read_text() == "__version__ = '1'\n"
    assert not (root / "musicdl" / "removed_module.py").exists()
    assert (root / "README.md").read_text() == "readme 1"
    assert (root / ".venv" / "keep.txt").read_text() == "venv"
    assert (root / ".env").read_text() == "SPOTIFY_CLIENT_ID=secret"
    assert pip_calls == []  # pyproject unchanged
    assert updater.read_state(root)["sha"] == SHA1
    assert not [p for p in root.iterdir() if p.name.startswith(".update-") or p.name.endswith((".new", ".old"))]

    # same commit again: nothing to do, no download
    github.calls.clear()
    assert updater.check_and_update(fetch=github, root=root, pip=pip_calls.append) is False
    assert github.calls == [updater.API_URL]


def test_dependency_change_runs_pip_and_retries_on_failure(tmp_path):
    root = installed(tmp_path)
    github = FakeGitHub(SHA2, release("2", pyproject="deps-2"))
    results = iter([False, True])
    pip_calls = []

    def pip(path):
        pip_calls.append(path)
        return next(results)

    assert updater.check_and_update(fetch=github, root=root, pip=pip) is True
    assert pip_calls == [root]
    assert updater.read_state(root)["pip_failed"] is True

    assert updater.check_and_update(fetch=github, root=root, pip=pip) is False
    assert len(pip_calls) == 2
    assert updater.read_state(root)["pip_failed"] is False


def test_offline_or_broken_archive_changes_nothing(tmp_path):
    root = installed(tmp_path)
    github = FakeGitHub(SHA1, release("1"))
    github.offline = True
    assert updater.check_and_update(fetch=github, root=root) is False

    broken = FakeGitHub(SHA1, {"something/else.txt": "x"})
    assert updater.check_and_update(fetch=broken, root=root) is False
    assert (root / "musicdl" / "__init__.py").read_text() == "__version__ = 'old'\n"
    assert updater.read_state(root) == {}


def test_not_updating_dev_checkout(tmp_path):
    assert updater.check_and_update(fetch=lambda *a: (_ for _ in ()).throw(AssertionError()), root=None) in (False,)


def test_version_label_and_pycache_kept(tmp_path):
    root = installed(tmp_path)
    cache = root / "musicdl" / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"pyc")
    assert updater.version_label(root) == "версия не определена"
    assert updater.check_and_update(fetch=FakeGitHub(SHA1, release("1")), root=root) is True
    assert updater.version_label(root) == "версия от 01.10.2026 11:05 UTC"
    assert (cache / "x.pyc").exists()  # never touched (may be in use)
    assert not list(root.rglob("*.new"))
