from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .profiles import Profile
from .transport import WTITransport
from .watcher import BannerWatcher


@dataclass
class Session:
    profile: Profile
    transport: WTITransport
    watcher: BannerWatcher
    last_error: Optional[str] = None
    log: list[str] = field(default_factory=list)


class SessionManager:
    """Holds the (at most one) active session.

    v1 is single-target at a time — keeps the UI and the abort logic simple.
    Multi-target can come later if needed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Session | None = None

    def current(self) -> Session | None:
        return self._session

    def open(self, profile: Profile) -> Session:
        with self._lock:
            if self._session is not None:
                raise RuntimeError(
                    f"a session is already open for {self._session.profile.name!r}; close it first"
                )
            transport = WTITransport(profile.wti_host, profile.wti_port)
            transport.open()
            watcher = BannerWatcher(transport, profile)
            watcher.start()
            self._session = Session(profile=profile, transport=transport, watcher=watcher)
            return self._session

    def close(self) -> None:
        with self._lock:
            sess = self._session
            self._session = None
        if sess is not None:
            sess.watcher.stop()
            sess.transport.close()
