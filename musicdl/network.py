"""Surviving a lost internet connection.

When the connection drops, searches and downloads fail with all kinds of
errors (DNS, timeouts, "Max retries exceeded"...). Instead of marking every
remaining track as failed, the job waits until the connection is back and
continues where it stopped.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Any of these answering means we are online (DNS + TCP).
PROBE_HOSTS = (("music.youtube.com", 443), ("www.google.com", 443), ("one.one.one.one", 443))
CHECK_INTERVAL = 5.0  # seconds between checks while offline

_NETWORK_HINTS = (
    "getaddrinfo",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "failed to establish a new connection",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection broken",
    "remote end closed connection",
    "remotedisconnected",
    "network is unreachable",
    "no route to host",
    "timed out",
    "timeout",
    "max retries exceeded",
    "unable to connect",
    "unable to download",
    "failed to complete request",
    "could not get client token",
    "errno 11001",  # Windows: host not found
    "errno 10051",  # Windows: network unreachable
    "errno 10060",  # Windows: timed out
    "errno 10054",  # Windows: reset by peer
)


def looks_like_network_error(exc: BaseException) -> bool:
    """True for errors that a lost connection typically produces."""
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout)):
            return True
        text = f"{type(current).__name__} {current}".lower()
        if any(hint in text for hint in _NETWORK_HINTS):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_online(timeout: float = 3.0) -> bool:
    for host, port in PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class ConnectionGuard:
    """Shared by all worker threads of one job: the first thread that notices
    the connection is gone reports it once, every thread waits until it is
    back (or the job is cancelled)."""

    def __init__(
        self,
        on_change: Callable[[bool], None] = lambda online: None,
        cancel: Optional[threading.Event] = None,
        check: Callable[[], bool] = is_online,
        interval: Optional[float] = None,
    ) -> None:
        self.on_change = on_change
        self.cancel = cancel or threading.Event()
        self.check = check
        self.interval = CHECK_INTERVAL if interval is None else interval
        self._lock = threading.Lock()
        self._offline = False

    @property
    def offline(self) -> bool:
        return self._offline

    def wait_until_online(self) -> bool:
        """Returns immediately when online. Otherwise blocks until the
        connection is back (True) or the job is cancelled (False)."""
        if not self._offline and self.check():
            return True
        with self._lock:
            if not self._offline:
                if self.check():
                    return True
                self._offline = True
                logger.warning("Internet connection lost, waiting")
                self.on_change(False)
        while not self.cancel.wait(self.interval):
            with self._lock:
                if not self._offline:
                    return True
                if self.check():
                    self._offline = False
                    logger.warning("Internet connection is back")
                    self.on_change(True)
                    return True
        return False
