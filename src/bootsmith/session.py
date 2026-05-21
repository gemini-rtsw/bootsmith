from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .profiles import Profile
from .transport import TelnetTransport
from .watcher import BannerWatcher


@dataclass
class Session:
    profile: Profile
    transport: TelnetTransport
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
        # WTI side may not have realized the prior session ended yet, or
        # the board may be mid-boot and not forwarding. Retry the connect
        # with backoff up to ~12s so the user doesn't have to spam the
        # button.
        import time as _t

        last_err: Optional[Exception] = None
        transport: Optional[TelnetTransport] = None
        for delay in (0.0, 1.0, 2.0, 3.0, 5.0):
            if delay:
                _t.sleep(delay)
            try:
                transport = TelnetTransport(profile.wti_host, profile.wti_port)
                transport.open(timeout=4.0)
                # Probe: a CR should provoke either a prompt echo OR the
                # WTI dropping us within ~1s if it doesn't actually want us.
                _t.sleep(0.3)
                try:
                    transport.write(b"\r")
                except Exception:
                    pass
                _t.sleep(1.5)
                if transport.status().connected:
                    break
                # WTI dropped us — close and retry.
                try:
                    transport.close()
                except Exception:
                    pass
                transport = None
            except Exception as e:
                last_err = e
                transport = None
        if transport is None:
            raise ConnectionError(
                f"could not get a stable connection to {profile.wti_host}:{profile.wti_port}"
                + (f" ({last_err})" if last_err else "")
            )
        watcher = BannerWatcher(transport, profile)
        watcher.start()
        with self._lock:
            self._session = Session(profile=profile, transport=transport, watcher=watcher)

        # Auto-prompt: nudge the board into echoing its prompt so the user
        # doesn't have to click anything after connecting. Try multiple
        # times because the IAC handshake timing varies. Stops as soon as
        # the watcher reports at_prompt.
        def _bump():
            import time as _t

            for delay in (0.8, 0.6, 1.0, 1.5):
                _t.sleep(delay)
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
