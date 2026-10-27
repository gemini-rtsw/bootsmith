from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .profiles import Profile, TerminalServer
from .transport import TelnetTransport
from .watcher import BannerWatcher

# A session whose browser has made no request for this long is closed so
# its WTI port is freed. An open page polls /session/status every 2 s, so
# only a closed or crashed tab gets anywhere near this.
IDLE_TIMEOUT_S = 10 * 60


def _new_push_job() -> dict:
    return {
        "id": 0,
        "state": "idle",        # idle | running | done | error
        "started_at": 0.0,
        "finished_at": 0.0,
        "verify_html": "",      # rendered _params_verify.html when done
        "error": "",
    }


def _new_cnfg_job() -> dict:
    return {
        "id": 0,
        "state": "idle",        # idle | running | done | error
        "started_at": 0.0,
        "finished_at": 0.0,
        "done_html": "",
        "error": "",
    }


@dataclass
class Session:
    profile: Profile
    terminal_server: TerminalServer
    transport: TelnetTransport
    watcher: BannerWatcher
    last_error: Optional[str] = None
    log: list[str] = field(default_factory=list)
    opened_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Per-session job state; each board gets its own push/CNFG slot.
    # push_lock serializes push/read-params on this transport so two
    # concurrent clicks never run two dialogue walks against it at once.
    push_lock: threading.Lock = field(default_factory=threading.Lock)
    push_job: dict = field(default_factory=_new_push_job)
    push_job_lock: threading.Lock = field(default_factory=threading.Lock)
    cnfg_job: dict = field(default_factory=_new_cnfg_job)
    cnfg_job_lock: threading.Lock = field(default_factory=threading.Lock)


class PortInUseError(ConnectionError):
    """The WTI port is already held by another browser's session."""


class SessionManager:
    """Holds one session per browser, keyed by a session id (cookie).

    A WTI port accepts one client, so a port can belong to only one
    session at a time; a second browser asking for it gets PortInUseError
    instead of knocking the first one off.
    """

    def __init__(self, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        # Ports being connected right now (connect takes seconds), so two
        # browsers clicking the same board at once can't both get through.
        self._connecting: dict[tuple[str, int], str] = {}
        self._idle_timeout = idle_timeout

    def current(self, sid: str) -> Session | None:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None:
                sess.last_seen = time.time()
            return sess

    def all(self) -> dict[str, Session]:
        with self._lock:
            return dict(self._sessions)

    def _owner_of(self, key: tuple[str, int]) -> tuple[str, Session | None] | None:
        """Caller holds self._lock."""
        for sid, sess in self._sessions.items():
            if (sess.terminal_server.host, sess.terminal_server.port) == key:
                return sid, sess
        if key in self._connecting:
            return self._connecting[key], None
        return None

    def open(self, sid: str, profile: Profile, terminal_server: TerminalServer) -> Session:
        self.reap_idle()
        key = (terminal_server.host, terminal_server.port)
        with self._lock:
            owner = self._owner_of(key)
            if owner is not None and owner[0] != sid:
                other = owner[1]
                if other is None:
                    raise PortInUseError(
                        f"{key[0]}:{key[1]} is being connected by another browser"
                    )
                raise PortInUseError(
                    f"{key[0]}:{key[1]} is in use by another browser "
                    f"({other.profile.name}, connected at "
                    f"{time.strftime('%H:%M', time.localtime(other.opened_at))}). "
                    "Disconnect it there first."
                )
            self._connecting[key] = sid
        try:
            # If this browser already has a session, close it cleanly first
            # so we don't leak a TCP socket every time the user clicks a
            # profile twice.
            self.close(sid)
            transport = self._connect(terminal_server)
            watcher = BannerWatcher(transport, profile)
            watcher.start()
            sess = Session(
                profile=profile,
                terminal_server=terminal_server,
                transport=transport,
                watcher=watcher,
            )
            with self._lock:
                self._sessions[sid] = sess
        finally:
            with self._lock:
                self._connecting.pop(key, None)

        # Auto-prompt: nudge the board into echoing its prompt so the user
        # doesn't have to click anything after connecting. Try multiple
        # times because the IAC handshake timing varies. Stops as soon as
        # the watcher reports at_prompt.
        def _bump():
            for delay in (0.8, 0.6, 1.0, 1.5):
                time.sleep(delay)
                if watcher.status().state == "at_prompt":
                    return
                try:
                    transport.write(b"\r")
                    time.sleep(0.5)
                    watcher.force_prompt()
                except Exception:
                    return

        threading.Thread(target=_bump, name="auto-prompt", daemon=True).start()
        return sess

    @staticmethod
    def _connect(terminal_server: TerminalServer) -> TelnetTransport:
        # WTI side may not have realized the prior session ended yet, or
        # the board may be mid-boot and not forwarding. Retry the connect
        # with backoff up to ~12s so the user doesn't have to spam the
        # button.
        last_err: Optional[Exception] = None
        for delay in (0.0, 1.0, 2.0, 3.0, 5.0):
            if delay:
                time.sleep(delay)
            try:
                transport = TelnetTransport(terminal_server.host, terminal_server.port)
                transport.open(timeout=4.0)
                # Probe: a CR should provoke either a prompt echo OR the
                # WTI dropping us within ~1s if it doesn't actually want us.
                time.sleep(0.3)
                try:
                    transport.write(b"\r")
                except Exception:
                    pass
                time.sleep(1.5)
                if transport.status().connected:
                    return transport
                # WTI dropped us — close and retry.
                try:
                    transport.close()
                except Exception:
                    pass
            except Exception as e:
                last_err = e
        raise ConnectionError(
            f"could not get a stable connection to {terminal_server.host}:{terminal_server.port}"
            + (f" ({last_err})" if last_err else "")
        )

    def close(self, sid: str) -> None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is not None:
            sess.watcher.stop()
            sess.transport.close()

    def reap_idle(self) -> list[str]:
        """Close sessions whose browser has gone quiet. Never reaps a
        session mid-push, so a slow push can't lose its transport."""
        cutoff = time.time() - self._idle_timeout
        with self._lock:
            stale = [
                sid for sid, s in self._sessions.items()
                if s.last_seen < cutoff and not s.push_lock.locked()
            ]
        for sid in stale:
            self.close(sid)
        return stale
