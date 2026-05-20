from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .patterns import ALL, LoaderPatterns, with_overrides
from .profiles import Profile
from .transport import WTITransport


# State strings exposed to the UI. Kept as bare strings instead of Enum so
# they serialize cleanly through Flask's jsonify without extra plumbing.
STATE_IDLE = "idle"             # not running
STATE_WAITING = "waiting"       # connected, watching for banner
STATE_ABORTING = "aborting"     # banner seen, spamming abort key
STATE_AT_PROMPT = "at_prompt"   # prompt matched, loader identified
STATE_MISSED = "missed"         # banner seen but never reached prompt — user must retry


@dataclass
class WatcherStatus:
    state: str = STATE_IDLE
    loader: Optional[str] = None
    last_banner_at: Optional[float] = None
    last_prompt_at: Optional[float] = None
    abort_chars_sent: int = 0
    notes: list[str] = field(default_factory=list)


class BannerWatcher:
    """Background watcher that catches a reboot, spams abort, lands at prompt.

    Wire-up:
        w = BannerWatcher(transport, profile)
        w.start()
        ... w.status() ...
        w.stop()

    The watcher runs a small state machine. It does NOT own the transport's
    read loop; it just subscribes to bytes the transport pushes.
    """

    # Hard limits so we don't loop forever in a broken state.
    _ABORT_BURST_INTERVAL = 0.05      # 20 chars/sec while in ABORTING
    _ABORT_TIMEOUT_S = 12.0           # if no prompt within 12s of banner, declare miss
    _PROMPT_SCAN_WINDOW = 4096        # bytes of recent stream kept for prompt match

    def __init__(
        self,
        transport: WTITransport,
        profile: Profile,
        on_state_change: Optional[Callable[[WatcherStatus], None]] = None,
    ):
        self._transport = transport
        self._profile = profile
        self._on_state_change = on_state_change
        self._status = WatcherStatus(state=STATE_IDLE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Build effective patterns: defaults + per-profile overrides.
        self._patterns: list[LoaderPatterns] = []
        loader_hint = (profile.loader_hint or "auto").lower()
        for base in ALL:
            if loader_hint != "auto" and base.name != loader_hint:
                # Hinted to a specific loader — skip the other entirely so we
                # don't accidentally detect the wrong one on noisy serial.
                continue
            override = {}
            if base.name in profile.banners:
                override["banner"] = profile.banners[base.name]
            if base.name in profile.prompts:
                override["prompt"] = profile.prompts[base.name]
            if "abort_" + base.name in profile.banners:
                override["abort"] = profile.banners["abort_" + base.name]
            self._patterns.append(with_overrides(base, override))

    def status(self) -> WatcherStatus:
        with self._lock:
            return WatcherStatus(
                state=self._status.state,
                loader=self._status.loader,
                last_banner_at=self._status.last_banner_at,
                last_prompt_at=self._status.last_prompt_at,
                abort_chars_sent=self._status.abort_chars_sent,
                notes=list(self._status.notes),
            )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state(STATE_WAITING)
        self._thread = threading.Thread(
            target=self._run, name=f"banner-watcher-{self._profile.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._set_state(STATE_IDLE)

    def rearm(self) -> None:
        """Reset to WAITING after a miss so user can reboot again."""
        with self._lock:
            self._status = WatcherStatus(state=STATE_WAITING)
        if self._on_state_change:
            self._on_state_change(self.status())

    def force_prompt(self) -> None:
        """Skip banner detection — assume the board is already at a prompt.

        Caller is responsible for sending a CR via the transport before/after
        as needed. We just transition state so the UI moves forward.
        """
        # Try to match a prompt from the current ring buffer right away.
        loader = self._match_prompt(self._transport.snapshot())
        if loader is not None:
            with self._lock:
                self._status.loader = loader
                self._status.last_prompt_at = time.time()
            self._set_state(STATE_AT_PROMPT)

    def _run(self) -> None:
        q = self._transport.subscribe()
        buf = bytearray()
        active: Optional[LoaderPatterns] = None
        banner_at: Optional[float] = None
        last_abort = 0.0
        try:
            while not self._stop.is_set():
                # Drain queued chunks into the local scan buffer.
                made_progress = False
                while q:
                    chunk = q.popleft()
                    buf.extend(chunk)
                    made_progress = True
                # Cap scan window — only recent bytes matter for prompt/banner.
                if len(buf) > self._PROMPT_SCAN_WINDOW:
                    del buf[: len(buf) - self._PROMPT_SCAN_WINDOW]

                state = self.status().state

                if state == STATE_WAITING:
                    if made_progress:
                        hit = self._match_banner(buf)
                        if hit is not None:
                            active = hit
                            banner_at = time.time()
                            with self._lock:
                                self._status.loader = hit.name
                                self._status.last_banner_at = banner_at
                            self._set_state(STATE_ABORTING)
                            # First abort immediately — don't wait the interval.
                            self._send_abort(active)
                            last_abort = time.time()

                elif state == STATE_ABORTING:
                    assert active is not None and banner_at is not None
                    # Check for prompt arrival. Don't gate on `made_progress` —
                    # the prompt may have arrived in the same chunk as the
                    # banner, in which case the WAITING branch transitioned us
                    # to ABORTING but never got to scan for the prompt.
                    if self._match_specific_prompt(buf, active):
                        with self._lock:
                            self._status.last_prompt_at = time.time()
                        self._set_state(STATE_AT_PROMPT)
                        active = None
                        banner_at = None
                        continue
                    # Spam abort at fixed interval.
                    now = time.time()
                    if now - last_abort >= self._ABORT_BURST_INTERVAL:
                        self._send_abort(active)
                        last_abort = now
                    # Timeout?
                    if now - banner_at > self._ABORT_TIMEOUT_S:
                        with self._lock:
                            self._status.notes.append(
                                f"missed abort window for {active.name} after {self._ABORT_TIMEOUT_S:.0f}s"
                            )
                        self._set_state(STATE_MISSED)
                        active = None
                        banner_at = None

                elif state == STATE_AT_PROMPT or state == STATE_MISSED:
                    # Nothing to do; wait for external action (rearm / stop).
                    pass

                if not made_progress:
                    time.sleep(0.02)
        finally:
            self._transport.unsubscribe(q)

    # --- pattern helpers ---

    def _match_banner(self, buf: bytes) -> Optional[LoaderPatterns]:
        for p in self._patterns:
            if p.banner.search(buf):
                return p
        return None

    def _match_specific_prompt(self, buf: bytes, p: LoaderPatterns) -> bool:
        # Match against the tail of the buffer — prompts only matter at end of line.
        tail = buf[-256:] if len(buf) > 256 else bytes(buf)
        return p.prompt.search(tail) is not None

    def _match_prompt(self, buf: bytes) -> Optional[str]:
        tail = buf[-256:] if len(buf) > 256 else bytes(buf)
        for p in self._patterns:
            if p.prompt.search(tail):
                return p.name
        return None

    # --- effects ---

    def _send_abort(self, p: LoaderPatterns) -> None:
        try:
            self._transport.write(p.abort)
        except Exception as e:
            with self._lock:
                self._status.notes.append(f"abort write failed: {e}")
            return
        with self._lock:
            self._status.abort_chars_sent += len(p.abort)

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            if self._status.state == new_state:
                return
            self._status.state = new_state
        if self._on_state_change:
            self._on_state_change(self.status())
