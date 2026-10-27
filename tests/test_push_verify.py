"""VxWorks push verify: only flag fields the `c` dialogue wrote.

Reproduces MKO-PROD-GNIRSCC: an mv167 boot ROM has no "unit number"
field, so a profile carrying unit_number=0 always read it back blank
and the verify panel reported "unit_number: wrote 0, read back".
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from bootsmith import vxworks as vxworks_mod
from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer

PARAMS = {
    "boot_device": "ei",
    "processor_number": "0",
    "host_name": "mkotcsbootv2-lv1",
    "host_inet": "10.2.2.145",
    "target_name": "gnirscc",
}


class PushVerifyTests(unittest.TestCase):
    def _push(self, boot_params, written, read_back):
        app = create_app()
        profile = Profile(
            name="MKO-PROD-GNIRSCC",
            loader_hint="vxworks",
            terminal_servers=[TerminalServer(name="production", host="10.2.2.86", port=10003)],
            boot_params=boot_params,
        )
        sess = SimpleNamespace(
            profile=profile,
            transport=object(),
            watcher=SimpleNamespace(
                status=lambda: SimpleNamespace(state="at_prompt", loader="vxworks")
            ),
        )
        app.config["sessions"].current = lambda: sess
        client = app.test_client()
        with mock.patch.object(
            vxworks_mod,
            "write_params",
            return_value=vxworks_mod.WriteResult(raw=b"", fields_written=written),
        ), mock.patch.object(
            vxworks_mod,
            "read_params",
            return_value=vxworks_mod.ReadResult(params=read_back, raw=b""),
        ):
            self.assertEqual(client.post("/params/push").status_code, 200)
            for _ in range(100):
                if app.config["push_job"]["state"] != "running":
                    break
                time.sleep(0.02)
        self.assertEqual(app.config["push_job"]["state"], "done")
        return client.get("/params/push/status").get_data(as_text=True)

    def test_field_missing_from_board_is_not_flagged(self):
        html = self._push(
            boot_params={**PARAMS, "unit_number": "0"},
            written=list(PARAMS),          # c dialogue never prompts unit number
            read_back=dict(PARAMS),        # p listing has no unit number either
        )
        self.assertNotIn("did not read back", html)
        self.assertNotIn("unit_number", html)

    def test_real_mismatch_is_still_flagged(self):
        html = self._push(
            boot_params=dict(PARAMS),
            written=list(PARAMS),
            read_back={**PARAMS, "host_name": "pisces-control"},
        )
        self.assertIn("did not read back", html)
        self.assertIn("host_name", html)


if __name__ == "__main__":
    unittest.main()
