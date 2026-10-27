"""Connect feedback: failed requests must reach the page.

htmx does not swap 4xx/5xx responses, so the page relies on the
#flash banner (index.html) to show _error() bodies. These tests pin the
server side of that contract: a failed connect returns an escaped
error <div>, and the Connect forms carry the in-flight feedback hooks.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from bootsmith import profiles as profiles_mod
from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer
from bootsmith.session import SessionManager


class ConnectFeedbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = profiles_mod.profile_dir()
        profiles_mod.set_profile_dir(self._tmp.name)
        profiles_mod.save_profile(
            Profile(
                name="MCS",
                loader_hint="vxworks",
                terminal_servers=[
                    TerminalServer(name="production", host="10.1.5.85", port=2308)
                ],
            )
        )
        self.client = create_app().test_client()

    def tearDown(self):
        profiles_mod.set_profile_dir(self._old_dir)
        self._tmp.cleanup()

    def test_blocked_connect_returns_error_div(self):
        err = ConnectionError(
            "could not get a stable connection to 10.1.5.85:2308 (<refused>)"
        )
        with mock.patch.object(SessionManager, "open", side_effect=err):
            resp = self.client.post(
                "/session/open",
                data={"name": "MCS", "terminal_server": "production"},
            )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith('<div class="error">'))
        self.assertIn("could not get a stable connection to 10.1.5.85:2308", body)
        # Exception text is escaped, not injected as markup.
        self.assertIn("&lt;refused&gt;", body)
        self.assertNotIn("<refused>", body)

    def test_page_has_flash_and_connect_feedback(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="flash"', body)
        self.assertIn("htmx:responseError", body)
        self.assertIn('hx-disabled-elt="find button"', body)
        self.assertIn("Connecting&hellip;", body)


if __name__ == "__main__":
    unittest.main()
