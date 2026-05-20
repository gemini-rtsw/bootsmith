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
        # If a session is already open, close it cleanly first so we don't
        # leak a TCP socket every time the user clicks a profile twice.
        existing = self._session
        if existing is not None:
            self.close()
        transport = WTITransport(profile.wti_host, profile.wti_port)
        transport.open()
        watcher = BannerWatcher(transport, profile)
        watcher.start()
        with self._lock:
            self._session = Session(profile=profile, transport=transport, watcher=watcher)

        # Auto-prompt: after IAC negotiation settles, try to surface the
        # loader prompt so the user sees something immediately.
        #
        # First we send ^D in case the previous user left the board mid-`c`
        # dialogue — ^D bails out of the dialogue without committing. If the
        # board is already at the prompt, ^D is harmless. Then a CR makes
        # the board echo the prompt. We retry once because some boards take
        # a beat after IAC to start responding.
        def _bump():
            import time as _t

            _t.sleep(0.8)
            for delay in (0.0, 0.6):
                if delay:
                    _t.sleep(delay)
                try:
                    transport.write(b"\x04")  # ^D — quit any open c dialogue
                    _t.sleep(0.15)
                    transport.write(b"\r")
                    _t.sleep(0.4)
                    watcher.force_prompt()
                except Exception:
                    return
                if watcher.status().state == "at_prompt":
                    return

        threading.Thread(target=_bump, name="auto-prompt", daemon=True).start()
        return self._session

    def close(self) -> None:
        with self._lock:
            sess = self._session
            self._session = None
        if sess is not None:
            sess.watcher.stop()
            sess.transport.close()
