"""The link between the new window and the existing engine (``musicdl.job``).

This is the *only* webui module that touches ``musicdl.job`` and friends, and
it only uses their public surface:

    job.JobOptions(...)        job.JobEvents(...)         job.run_job(options, events)
    job.JobError               matching.CANCELLED         pipeline.expected_path(song, out_dir)
    spotify_input.check_url    updater.version_label

``tests/test_webui.py::test_engine_contract`` checks these, so when the engine
changes, the test says exactly what to adjust here.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class RealEngine:
    name = "real"

    def __init__(self, run_job: Optional[Callable] = None) -> None:
        self._run_job = run_job  # tests inject a run_job with stub providers

    # -- running ------------------------------------------------------------
    def run(self, options: Dict[str, Any], cancel: Any, handlers: Dict[str, Callable]) -> Any:
        from musicdl import job

        # Only hook the callbacks this version of the engine has: a callback
        # added or removed there must not break the window.
        known = {field.name for field in dataclasses.fields(job.JobEvents)}
        events = job.JobEvents(**{name: handler for name, handler in handlers.items() if name in known})
        job_options = job.JobOptions(
            source_kind=options["source_kind"],
            source=options["source"],
            out_dir=Path(options["out_dir"]),
            threads=int(options["threads"]),
            bitrate=options["bitrate"],
            dry_run=bool(options["dry_run"]),
            only_verified=bool(options["only_verified"]),
            env_file=Path.cwd() / ".env",
            cancel=cancel,
        )
        return (self._run_job or job.run_job)(job_options, events)

    # -- small questions the session asks ------------------------------------
    def is_user_error(self, exc: BaseException) -> bool:
        from musicdl.job import JobError

        return isinstance(exc, JobError)

    def is_cancelled(self, match: Any) -> bool:
        from musicdl.matching import CANCELLED

        return match.error == CANCELLED

    def is_not_found(self, match: Any) -> bool:
        return match.error == "not found"

    def existing_name(self, song: Any, out_dir: str) -> str:
        try:
            from musicdl.pipeline import expected_path

            return expected_path(song, Path(out_dir)).name
        except Exception:
            return f"{song.artist} - {song.name}.mp3"

    # -- input checks ----------------------------------------------------------
    def check_link(self, link: str) -> Optional[str]:
        """-> 'track' | 'album' | 'playlist', or None if it is not a Spotify link."""
        from musicdl.spotify_input import SpotifyInputError, check_url

        try:
            return check_url(link)
        except SpotifyInputError:
            return None

    def version(self) -> str:
        try:
            from musicdl.updater import version_label

            return version_label()
        except Exception:
            return ""
