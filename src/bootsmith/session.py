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

        # Auto-prompt: after IAC negotiation settles, send a single CR to
        # nudge the board into echoing whatever prompt it has. Runs once
        # and exits. No retries, no ^D — both caused races with in-flight
        # Push-to-board c-dialogues (rogue ^D in the middle of a field).
        # If the user lands on a board mid-dialogue or with no prompt
        # visible, the Force prompt button is one click away.
        def _bump():
            import time as _t

            _t.sleep(0.8)
            if watcher.status().state == "at_prompt":
                return
            try:
                transport.write(b"\r")
                _t.sleep(0.5)
                watcher.force_prompt()
            except Exception:
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
