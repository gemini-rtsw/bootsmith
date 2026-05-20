"""Parser + dialogue tests for the VxWorks driver.

The parser tests cover static parsing of `p` output. The dialogue tests
drive `write_params` against a FakeTransport that scripts the prompts the
boot ROM would print, and assert that the right bytes get written for
each field semantic (keep / clear / set).
"""

from __future__ import annotations

import threading
import time
import unittest
from collections import deque

from bootsmith.vxworks import FIELDS_WITH_UNIT, _parse_print, write_params


SAMPLE = (
    b"boot device          : geisc\r\n"
    b"unit number          : 0\r\n"
    b"processor number     : 0\r\n"
    b"host name            : mkogmosdev-lv1\r\n"
    b"file name            : /gemdev/vxworks/mv6100_314Test3/vxWorks.5\r\n"
    b"inet on ethernet (e) : 10.2.126.101:ffffff00\r\n"
    b"host inet (h)        : 10.2.126.21\r\n"
    b"user (u)             : gemdev\r\n"
    b"flags (f)            : 0x8\r\n"
    b"target name (tn)     : gmosdc\r\n"
    b"startup script (s)   : /gemdev/rt/gmosdc/bin/vxWorks-ppc604_long/startup\r\n"
    b"[VxWorks Boot]: "
)


class VxWorksParseTests(unittest.TestCase):
    def test_known_fields(self):
        d = _parse_print(SAMPLE)
        self.assertEqual(d["boot_device"], "geisc")
        self.assertEqual(d["unit_number"], "0")
        self.assertEqual(d["processor_number"], "0")
        self.assertEqual(d["host_name"], "mkogmosdev-lv1")
        self.assertEqual(d["file_name"], "/gemdev/vxworks/mv6100_314Test3/vxWorks.5")
        self.assertEqual(d["inet_on_ethernet"], "10.2.126.101:ffffff00")
        self.assertEqual(d["host_inet"], "10.2.126.21")
        self.assertEqual(d["user"], "gemdev")
        self.assertEqual(d["flags"], "0x8")
        self.assertEqual(d["target_name"], "gmosdc")
        self.assertEqual(
            d["startup_script"],
            "/gemdev/rt/gmosdc/bin/vxWorks-ppc604_long/startup",
        )

    def test_empty_input(self):
        self.assertEqual(_parse_print(b""), {})

    def test_extra_lines_ignored(self):
        # The `p` output is usually preceded by the echoed command, surrounded
        # by prompts, etc. Parser should ignore anything that doesn't match
        # a known label.
        noisy = b"\r\np\r\n" + SAMPLE + b"\r\n[VxWorks Boot]: "
        d = _parse_print(noisy)
        self.assertEqual(d.get("host_name"), "mkogmosdev-lv1")


class FakeTransport:
    """Quacks like WTITransport but lets a script feed bytes on demand."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[deque[bytes]] = []
        self.writes: list[bytes] = []

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

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def push(self, data: bytes) -> None:
        with self._lock:
            for q in self._subs:
                q.append(data)


def _run_dialogue(t: FakeTransport, values: dict[str, str]) -> None:
    """Drive write_params on its own thread, feeding scripted prompts.

    Mimics the board: when Bootsmith sends `c\r`, we start emitting the
    field prompts one at a time, waiting for Bootsmith to respond between
    each. After the last field we emit `[VxWorks Boot]: ` so the driver
    knows the dialogue closed.
    """
    done = threading.Event()
    err: list[BaseException] = []

    def runner():
        try:
            write_params(t, values, timeout_per_field=2.0)
        except BaseException as e:
            err.append(e)
        finally:
            done.set()

    th = threading.Thread(target=runner, daemon=True)
    th.start()

    # Wait for the driver to send `c\r`, then start the dialogue.
    deadline = time.time() + 2.0
    while not t.writes and time.time() < deadline:
        time.sleep(0.02)
    assert t.writes and t.writes[0] == b"c\r", f"first write was {t.writes!r}"

    # Walk each field. For each, push the prompt, then wait for the driver
    # to write something in response (the value or just `\r`).
    for i, (label, _key) in enumerate(FIELDS_WITH_UNIT):
        t.push(f"\r\n{label}          : current_{i} ".encode())
        # Wait for one more write to appear (the driver's response).
        target = len(t.writes) + 1
        d2 = time.time() + 2.0
        while len(t.writes) < target and time.time() < d2:
            time.sleep(0.01)
    # End of dialogue.
    t.push(b"\r\n[VxWorks Boot]: ")
    assert done.wait(timeout=3.0), "write_params did not return in time"
    if err:
        raise err[0]


class VxWorksDialogueTests(unittest.TestCase):
    def test_blank_means_keep_sends_only_cr(self):
        t = FakeTransport()
        _run_dialogue(t, values={})
        # First write is the `c\r` to enter dialogue; every subsequent write
        # should be just `\r` (keep current) for every field.
        self.assertEqual(t.writes[0], b"c\r")
        field_writes = t.writes[1 : 1 + len(FIELDS_WITH_UNIT)]
        self.assertEqual(
            field_writes,
            [b"\r"] * len(FIELDS_WITH_UNIT),
            f"writes were: {t.writes!r}",
        )

    def test_dot_clears_field(self):
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "."})
        host_idx = next(
            i for i, (_l, k) in enumerate(FIELDS_WITH_UNIT) if k == "host_name"
        )
        # +1 because writes[0] is the `c\r`.
        self.assertEqual(t.writes[1 + host_idx], b".\r")

    def test_value_sets_field(self):
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "mkogmosdev-lv1"})
        host_idx = next(
            i for i, (_l, k) in enumerate(FIELDS_WITH_UNIT) if k == "host_name"
        )
        # writes[0] is `c\r` to enter dialogue; per-field writes follow.
        self.assertEqual(t.writes[1 + host_idx], b"mkogmosdev-lv1\r")

    def test_other_fields_still_kept_when_one_set(self):
        # If you set host_name to a value but leave everything else blank,
        # every other field should still get just `\r` (keep current).
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "x"})
        non_host = [
            w for i, w in enumerate(t.writes[1 : 1 + len(FIELDS_WITH_UNIT)], start=0)
            if FIELDS_WITH_UNIT[i][1] != "host_name"
        ]
        self.assertTrue(
            all(w == b"\r" for w in non_host),
            f"non-host writes were: {non_host!r}",
        )


if __name__ == "__main__":
    unittest.main()
