"""Dialogue tests for the PPCBug driver, focused on CNFG;M VPD repair.

The regression these guard against: a board with a corrupt Board
Information Block prints field values full of `?` characters, e.g.

    Board (PWA) Serial Number = "????UUUUUUUU"?

The prompt detector used to grab the FIRST `?` on the line (one inside
the quoted value), never recognize a real prompt, time out, and escape
the whole CNFG;M dialogue with `.` -- which PPCBug treats as "abort,
save nothing". So the one situation CNFG;M repair exists for (corrupt
VPD) was exactly the one it couldn't handle.
"""

from __future__ import annotations

import threading
import time
import unittest
from collections import deque

from bootsmith.ppcbug import CNFG_FIELDS, write_cnfg


class FakeTransport:
    """Quacks like WTITransport; a script feeds bytes on demand."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[deque[bytes]] = []
        self.writes: list[bytes] = []

    def subscribe(self, seed_history: bool = True) -> deque[bytes]:
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

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def push(self, data: bytes) -> None:
        with self._lock:
            for q in self._subs:
                q.append(data)


# Corrupt VPD values, as the board actually prints them -- every one
# carries `?` and/or `U`/`5` garbage inside the displayed value.
_CORRUPT_VALUES = {
    "cnfg_board_sn": '"????UUUUUUUU"',
    "cnfg_board_id": '"UUUUUUUUUUUUUUUU"',
    "cnfg_artwork_id": '"UUUUUUUUUUUUUUUU"',
    "cnfg_mpu_clock": '"UUU"',
    "cnfg_bus_clock": '"UUU"',
    "cnfg_mac": "555555555555",
    "cnfg_scsi_id": '"UU"',
    "cnfg_system_sn": '"UUUU????????????"',
    "cnfg_system_id": '"???????????????????????????????"',
    "cnfg_license_id": '"?????????"',
}


def _run_cnfg(t: FakeTransport, values: dict[str, str]) -> None:
    """Drive write_cnfg on its own thread, scripting the board's prompts.

    For each CNFG field we emit `LABEL =<corrupt value>?` and wait for
    the driver to respond with a CR-terminated write before moving on.
    After the last field we emit the Update-NVRAM prompt, then PPC1-Bug>.
    """
    done = threading.Event()
    err: list[BaseException] = []

    def runner():
        try:
            write_cnfg(t, values, timeout=2.0)
        except BaseException as e:  # noqa: BLE001
            err.append(e)
        finally:
            done.set()

    th = threading.Thread(target=runner, daemon=True)
    th.start()

    # Wait for the driver to send `CNFG;M\r`.
    deadline = time.time() + 2.0
    while not t.writes and time.time() < deadline:
        time.sleep(0.02)
    assert t.writes and t.writes[0] == b"CNFG;M\r", f"first write: {t.writes!r}"

    t.push(b"WARNING: Board Information Block Checksum Error\r\n")
    for label, key in CNFG_FIELDS:
        prev = len(t.writes)
        t.push(f"{label} = {_CORRUPT_VALUES[key]}?".encode())
        d2 = time.time() + 5.0
        while time.time() < d2:
            recent = t.writes[prev:]
            if any(w.endswith(b"\r") for w in recent):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(
                f"driver never responded to {label!r}; writes: {t.writes!r}"
            )
        # Board echoes a newline before the next prompt.
        t.push(b"\r\n")
    t.push(b"Update Non-Volatile RAM (Y/N)? ")
    # Wait for the Y, then close.
    d3 = time.time() + 5.0
    while time.time() < d3 and not any(b"Y" in w for w in t.writes[-4:]):
        time.sleep(0.01)
    t.push(b"\r\nPPC1-Bug>")
    assert done.wait(timeout=8.0), "write_cnfg did not return in time"
    if err:
        raise err[0]


class PPCBugCnfgCorruptVpdTests(unittest.TestCase):
    def test_corrupt_value_prompt_is_recognized_not_aborted(self):
        """The headline regression: a `?`-laden value must not cause the
        driver to bail out of CNFG;M with a `.`."""
        t = FakeTransport()
        _run_cnfg(t, values={"cnfg_board_sn": "E12345"})
        joined = b"".join(t.writes)
        # The repair value must have been typed.
        self.assertIn(b"E12345\r", joined)
        # And the dialogue must NOT have been aborted with `.`.
        self.assertNotIn(b".\r", t.writes, f"aborted! writes: {t.writes!r}")

    def test_all_fields_walked_to_the_save_prompt(self):
        """Every field gets a response and the driver answers Y to save."""
        t = FakeTransport()
        _run_cnfg(t, values={"cnfg_mac": "00800F123456"})
        joined = b"".join(t.writes)
        self.assertIn(b"00800F123456\r", joined)
        # Y to Update Non-Volatile RAM.
        self.assertIn(b"Y\r", t.writes)
        self.assertNotIn(b".\r", t.writes)

    def test_blank_value_keeps_current_with_bare_cr(self):
        """A field with no supplied value is kept (bare CR), never `.`."""
        t = FakeTransport()
        _run_cnfg(t, values={})
        # One CR per field (keep), plus the Y at the end. No `.` anywhere.
        self.assertNotIn(b".\r", t.writes)
        self.assertIn(b"Y\r", t.writes)


if __name__ == "__main__":
    unittest.main()
