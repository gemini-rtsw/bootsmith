"""Parser tests for the VxWorks driver.

We don't exercise the dialogue here (that needs a real or fake transport);
just the static parsing of `p` output, which is the part most likely to
break silently when a board prints a slightly different label.
"""

from __future__ import annotations

import unittest

from bootsmith.vxworks import _parse_print


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


if __name__ == "__main__":
    unittest.main()
