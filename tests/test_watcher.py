"""Watcher state-machine tests with a fake transport.

These don't open sockets — they shove canned bytes through a FakeTransport
that mimics the bits of WTITransport the watcher relies on. That keeps the
tests fast and deterministic and lets us assert exactly what the watcher
would have written back to the WTI.
"""

from __future__ import annotations

import threading
import time
import unittest
from collections import deque

from bootsmith.profiles import Profile
from bootsmith.watcher import (
    STATE_ABORTING,
    STATE_AT_PROMPT,
    STATE_MISSED,
    STATE_WAITING,
    BannerWatcher,
)


class FakeTransport:
    """Quacks like WTITransport for watcher purposes only."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[deque[bytes]] = []
        self._buf = bytearray()
        self.writes: list[bytes] = []

    # transport API used by the watcher --------------------------------------
    def subscribe(self) -> deque[bytes]:
        q: deque[bytes] = deque()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: deque[bytes]) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    # test helpers ------------------------------------------------------------
    def push(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)
            for q in self._subs:
                q.append(data)


def _wait_for(predicate, timeout=2.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


PROFILE = Profile(name="t", wti_host="x", wti_port=1)


class WatcherTests(unittest.TestCase):
    def test_vxworks_banner_to_prompt(self):
        t = FakeTransport()
        w = BannerWatcher(t, PROFILE)
        w.start()
        try:
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_WAITING))

            # Simulate IOC output first — must NOT trigger anything.
            t.push(b"epics-ioc> heartbeat 1\r\n")
            time.sleep(0.1)
            self.assertEqual(w.status().state, STATE_WAITING)
            self.assertEqual(t.writes, [])

            # Reboot — VxWorks banner appears.
            t.push(b"\r\nVxWorks System Boot\r\n")
            t.push(b"Press any key to stop auto-boot...\r\n  7\r\n  6\r\n")

            self.assertTrue(
                _wait_for(lambda: w.status().state == STATE_ABORTING),
                f"watcher state was {w.status().state}",
            )
            self.assertEqual(w.status().loader, "vxworks")
            self.assertTrue(len(t.writes) >= 1)
            # All abort bytes should be spaces for VxWorks.
            self.assertTrue(all(b == b" " for b in t.writes))

            # Now the loader prompt arrives.
            t.push(b"\r\n[VxWorks Boot]: ")

            self.assertTrue(
                _wait_for(lambda: w.status().state == STATE_AT_PROMPT),
                f"watcher state was {w.status().state}",
            )
            self.assertEqual(w.status().loader, "vxworks")
        finally:
            w.stop()

    def test_ppcbug_banner_to_prompt(self):
        t = FakeTransport()
        w = BannerWatcher(t, PROFILE)
        w.start()
        try:
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_WAITING))
            t.push(b"\r\nSelf Test Passed\r\n")
            t.push(b"Copyright Motorola Inc. 1988 - 1997, All Rights Reserved\r\n")

            self.assertTrue(_wait_for(lambda: w.status().state == STATE_ABORTING))
            self.assertEqual(w.status().loader, "ppcbug")
            # PPCBug abort is Esc (0x1b).
            self.assertTrue(all(b == b"\x1b" for b in t.writes))

            t.push(b"\r\nPPC1-Bug>")

            self.assertTrue(_wait_for(lambda: w.status().state == STATE_AT_PROMPT))
            self.assertEqual(w.status().loader, "ppcbug")
        finally:
            w.stop()

    def test_missed_window_then_rearm(self):
        t = FakeTransport()
        # Speed up the timeout so this test doesn't take 12s.
        w = BannerWatcher(t, PROFILE)
        w._ABORT_TIMEOUT_S = 0.4  # type: ignore[attr-defined]
        w.start()
        try:
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_WAITING))
            t.push(b"VxWorks System Boot\r\n")
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_ABORTING))
            # Never send a prompt — should transition to MISSED after the (sped-up) timeout.
            self.assertTrue(
                _wait_for(lambda: w.status().state == STATE_MISSED, timeout=2.0),
                f"watcher state was {w.status().state}",
            )
            # Re-arm and verify we can catch the next reboot.
            w.rearm()
            self.assertEqual(w.status().state, STATE_WAITING)
            t.push(b"VxWorks System Boot\r\n[VxWorks Boot]: ")
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_AT_PROMPT))
        finally:
            w.stop()

    def test_loader_hint_skips_other(self):
        # Profile hinted to ppcbug — VxWorks banner must NOT trip the watcher.
        p = Profile(name="t", wti_host="x", wti_port=1, loader_hint="ppcbug")
        t = FakeTransport()
        w = BannerWatcher(t, p)
        w.start()
        try:
            self.assertTrue(_wait_for(lambda: w.status().state == STATE_WAITING))
            t.push(b"VxWorks System Boot\r\n[VxWorks Boot]: ")
            time.sleep(0.2)
            self.assertEqual(w.status().state, STATE_WAITING)
            self.assertEqual(t.writes, [])
        finally:
            w.stop()


if __name__ == "__main__":
    unittest.main()
