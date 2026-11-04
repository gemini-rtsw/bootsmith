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
# A tab that sent /session/leave (closed or navigated away) is dropped
# this long after, unless it makes another request first (a reload).
LEAVE_GRACE_S = 10


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
    # Set by leave() when the tab's page goes away; cleared by any later
    # request from the tab. reap_idle() drops it after LEAVE_GRACE_S.
    leaving_at: Optional[float] = None
    # Per-session job state; each board gets its own push/CNFG slot.
    # push_lock serializes push/read-params on this transport so two
    # concurrent clicks never run two dialogue walks against it at once.
    push_lock: threading.Lock = field(default_factory=threading.Lock)
    push_job: dict = field(default_factory=_new_push_job)
    push_job_lock: threading.Lock = field(default_factory=threading.Lock)
    cnfg_job: dict = field(default_factory=_new_cnfg_job)
    cnfg_job_lock: threading.Lock = field(default_factory=threading.Lock)


class PortInUseError(ConnectionError):
    """Another browser is still connecting to the WTI port."""


class ConnectCancelled(ConnectionError):
    """The browser cancelled its connect before it finished."""


class SessionManager:
    """Holds one session per browser tab, keyed by a session id.

    A WTI port accepts one client, so bootsmith holds one connection per
    port and shares it: a second tab or browser that connects to a port
    already open elsewhere joins that connection instead of being refused.
    Every viewer sees the same terminal output and can type into it, like
    a shared screen session. Each viewer keeps its own profile and its own
    push/CNFG job panels; the push lock is shared so two viewers can never
    run two dialogue walks on the line at once. The connection closes when
    its last viewer disconnects or goes idle.
    """

    def __init__(
        self, idle_timeout: float = IDLE_TIMEOUT_S, leave_grace: float = LEAVE_GRACE_S
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        # Ports being connected right now (connect takes seconds), so two
        # browsers clicking the same board at once can't both get through.
        self._connecting: dict[tuple[str, int], str] = {}
        # One cancel flag per browser with a connect in flight; set by
        # cancel_connect() so the retry loop stops and frees the port.
        self._cancels: dict[str, threading.Event] = {}
        self._idle_timeout = idle_timeout
        self._leave_grace = leave_grace

    def current(self, sid: str) -> Session | None:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None:
                sess.last_seen = time.time()
                sess.leaving_at = None
            return sess

    def leave(self, sid: str) -> bool:
        """The tab's page is going away (closed, reload, navigation).

        Marks the viewer leaving; reap_idle() closes it after the grace
        unless the tab makes another request first. Without this a closed
        tab kept its share of the WTI port for IDLE_TIMEOUT_S, so the other
        tab's Disconnect left the line open (2026-11-04).
        """
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return False
            sess.leaving_at = time.time()
            return True

    def all(self) -> dict[str, Session]:
        with self._lock:
            return dict(self._sessions)

    def viewers(self, sess: Session) -> int:
        """Number of tabs/browsers sharing this session's connection."""
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.transport is sess.transport)

    def port_viewers(self) -> dict[tuple[str, int], int]:
        """Viewer count per open port, for the target list."""
        with self._lock:
            out: dict[tuple[str, int], int] = {}
            for sess in self._sessions.values():
                key = (sess.terminal_server.host, sess.terminal_server.port)
                out[key] = out.get(key, 0) + 1
            return out

    def set_profile(self, sid: str, profile: Profile) -> Session:
        """Switch this viewer to another preset on the same connection.

        The connection is the board, so a different preset only changes
        what this tab views and writes; the WTI line, watcher and other
        viewers are untouched. The preset must list this port.
        """
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                raise LookupError("no session open")
            key = (sess.terminal_server.host, sess.terminal_server.port)
            ts = next(
                (t for t in profile.terminal_servers if (t.host, t.port) == key), None
            )
            if ts is None:
                raise ValueError(
                    f"preset {profile.name} does not use {key[0]}:{key[1]}"
                )
            sess.profile = profile
            sess.terminal_server = ts
            return sess

    def _other_on(self, key: tuple[str, int], sid: str) -> Session | None:
        """A session of another viewer on this port. Caller holds self._lock."""
        for other_sid, sess in self._sessions.items():
            if other_sid != sid and (sess.terminal_server.host, sess.terminal_server.port) == key:
                return sess
        return None

    def _release(self, sess: Session) -> None:
        """Close sess's connection unless another viewer still uses it.
        Call after removing sess from self._sessions."""
        with self._lock:
            shared = any(s.transport is sess.transport for s in self._sessions.values())
        if not shared:
            sess.watcher.stop()
            sess.transport.close()

    def open(self, sid: str, profile: Profile, terminal_server: TerminalServer) -> Session:
        self.reap_idle()
        key = (terminal_server.host, terminal_server.port)
        with self._lock:
            other = self._other_on(key, sid)
            if other is not None:
                # Join the open connection rather than refuse or steal it.
                mine = self._sessions.pop(sid, None)
                sess = Session(
                    profile=profile,
                    terminal_server=terminal_server,
                    transport=other.transport,
                    watcher=other.watcher,
                    push_lock=other.push_lock,
                )
                self._sessions[sid] = sess
            else:
                mine = None
                connecting = self._connecting.get(key)
                if connecting is not None and connecting != sid:
                    raise PortInUseError(
                        f"{key[0]}:{key[1]} is being connected in another tab or browser; "
                        "try again in a few seconds"
                    )
                self._connecting[key] = sid
                cancel = threading.Event()
                self._cancels[sid] = cancel
        if other is not None:
            if mine is not None:
                self._release(mine)
            return sess
        try:
            # If this browser already has a session, close it cleanly first
            # so we don't leak a TCP socket every time the user clicks a
            # profile twice.
            self.close(sid)
            transport = self._connect(terminal_server, cancel)
            if cancel.is_set():
                transport.close()
                raise ConnectCancelled("Connect cancelled.")
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
                if self._cancels.get(sid) is cancel:
                    del self._cancels[sid]

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

    def cancel_connect(self, sid: str) -> bool:
        """Stop this browser's in-flight connect. True if one was running."""
        with self._lock:
            cancel = self._cancels.get(sid)
        if cancel is None:
            return False
        cancel.set()
        return True

    @staticmethod
    def _connect(
        terminal_server: TerminalServer, cancel: Optional[threading.Event] = None
    ) -> TelnetTransport:
        # WTI side may not have realized the prior session ended yet, or
        # the board may be mid-boot and not forwarding. Retry the connect
        # with backoff up to ~12s so the user doesn't have to spam the
        # button.
        last_err: Optional[Exception] = None
        cancel = cancel or threading.Event()
        for delay in (0.0, 1.0, 2.0, 3.0, 5.0):
            if cancel.wait(delay):
                raise ConnectCancelled("Connect cancelled.")
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
        """Detach this viewer; the connection closes with its last viewer."""
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is not None:
            self._release(sess)

    def reap_idle(self) -> list[str]:
        """Close sessions whose browser has gone quiet. Never reaps a
        session mid-push, so a slow push can't lose its transport."""
        now = time.time()
        cutoff = now - self._idle_timeout
        left = now - self._leave_grace
        with self._lock:
            stale = [
                sid for sid, s in self._sessions.items()
                if (s.last_seen < cutoff or (s.leaving_at is not None and s.leaving_at < left))
                and not s.push_lock.locked()
            ]
        for sid in stale:
            self.close(sid)
        return stale
