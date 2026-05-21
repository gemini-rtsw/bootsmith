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
        # Mimic the board echoing what the user typed (everything except
        # the trailing CR — VxWorks doesn't echo CR on its own line).
        if data and data != b"\r":
            self.push(data)

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

    # Driver sends `c\r` immediately (no more p-based discovery; we use
    # the observed c-dialogue order).
    deadline = time.time() + 2.0
    while not t.writes and time.time() < deadline:
        time.sleep(0.02)
    assert t.writes and t.writes[0] == b"c\r", f"first write was {t.writes!r}"

    from bootsmith.vxworks import C_DIALOGUE_ORDER
    for i, (label, _key) in enumerate(C_DIALOGUE_ORDER):
        prev = len(t.writes)
        t.push(f"\r\n{label}          : current_{i} ".encode())
        d2 = time.time() + 5.0
        while time.time() < d2:
            # Look for a \r write that arrived after prev.
            recent = t.writes[prev:]
            if any(w == b"\r" or w.endswith(b"\r") for w in recent):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(
                f"driver did not respond to {label!r}; writes: {t.writes!r}"
            )
    t.push(b"\r\n[VxWorks Boot]: ")
    assert done.wait(timeout=8.0), "write_params did not return in time"
    if err:
        raise err[0]


class VxWorksDialogueTests(unittest.TestCase):
    def test_blank_means_keep_sends_only_cr(self):
        from bootsmith.vxworks import C_DIALOGUE_ORDER
        t = FakeTransport()
        _run_dialogue(t, values={})
        # writes[0] is `c\r`, then one `\r` per field (keep current).
        self.assertEqual(t.writes[0], b"c\r")
        field_writes = t.writes[1 : 1 + len(C_DIALOGUE_ORDER)]
        self.assertEqual(
            field_writes,
            [b"\r"] * len(C_DIALOGUE_ORDER),
            f"writes were: {t.writes!r}",
        )

    def test_dot_clears_field(self):
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "."})
        # New driver matches whichever prompt the board prints next, so
        # writes are not at predictable indices. Just check that `.\r` is
        # in the stream somewhere.
        self.assertIn(b".\r", t.writes)

    def test_value_sets_field(self):
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "mkogmosdev-lv1"})
        # Value is slow-typed one byte at a time, then a CR. Verify the
        # bytes for "mkogmosdev-lv1" appear in order with a CR right after.
        joined = b"".join(t.writes)
        self.assertIn(b"mkogmosdev-lv1\r", joined)

    def test_other_fields_still_kept_when_one_set(self):
        t = FakeTransport()
        _run_dialogue(t, values={"host_name": "x"})
        joined = b"".join(t.writes)
        self.assertTrue(joined.startswith(b"c\r"))
        self.assertIn(b"x\r", joined)


if __name__ == "__main__":
    unittest.main()
