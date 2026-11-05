"""Reboot button: per-preset reboot keys, sent only from the running OS."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bootsmith import profiles as profiles_mod
from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer, reboot_bytes, reboot_spec
from bootsmith.session import Session
from bootsmith.watcher import BannerWatcher, STATE_AT_PROMPT, STATE_RUNNING, STATE_WAITING

from test_push_after_boot import FakeBoard

TS = TerminalServer(name="production", host="10.2.2.86", port=10003)


class RebootSpecTests(unittest.TestCase):
    def test_caret_is_a_control_key_and_text_gets_enter(self):
        self.assertEqual(reboot_bytes("^X"), b"\x18")
        self.assertEqual(reboot_bytes("^x"), b"\x18")
        self.assertEqual(reboot_bytes("^["), b"\x1b")
        self.assertEqual(reboot_bytes("exit"), b"exit\r")
        self.assertEqual(reboot_bytes(" dbpf ag:iocStats:SysReset 1 "),
                         b"dbpf ag:iocStats:SysReset 1\r")

    def test_bad_specs_are_refused(self):
        for bad in ("", "^", "^1", "a\tb", "café"):
            with self.assertRaises(ValueError, msg=bad):
                reboot_bytes(bad)

    def test_defaults(self):
        self.assertEqual(reboot_spec(Profile("A", loader_hint="vxworks")), "^X")
        self.assertEqual(reboot_spec(Profile("A", loader_hint="ppcbug")), "")
        self.assertEqual(reboot_spec(Profile("A", loader_hint="ppcbug", reboot="exit")), "exit")


class _Board(FakeBoard):
    """FakeBoard whose reboot keys take it back to the boot ROM."""

    def write(self, data: bytes):
        super().write(data)
        if data in (b"\x18", b"exit\r"):
            self.at_prompt = True


class RebootRouteTests(unittest.TestCase):
    def _session(self, loader="vxworks", reboot="", at_prompt=False):
        app = create_app()
        profile = Profile("MKO-PROD-X", [TS], loader, system="X", reboot=reboot)
        self.board = _Board()
        self.board.at_prompt = at_prompt
        self.watcher = BannerWatcher(self.board, profile)
        self.watcher._set_state(STATE_AT_PROMPT if at_prompt else STATE_RUNNING)
        self.sess = Session(profile=profile, terminal_server=TS,
                            transport=self.board, watcher=self.watcher)
        app.config["sessions"].current = lambda sid: self.sess
        return app.test_client()

    def test_vxworks_default_sends_ctrl_x_after_probe(self):
        r = self._session().post("/session/reboot")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.board.sent, [b"\r", b"\x18"])
        self.assertEqual(self.watcher.status().state, STATE_WAITING)
        self.assertFalse(self.sess.push_lock.locked())

    def test_rtems_command_is_typed_with_enter(self):
        r = self._session("ppcbug", reboot="exit").post("/session/reboot")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.board.sent, [b"\r", b"exit\r"])

    def test_rtems_without_reboot_sends_nothing(self):
        r = self._session("ppcbug").post("/session/reboot")
        self.assertEqual(r.status_code, 409)
        self.assertIn("no reboot set", r.get_data(as_text=True))
        self.assertEqual(self.board.sent, [])

    def test_refused_at_boot_prompt(self):
        r = self._session(at_prompt=True).post("/session/reboot")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.board.sent, [b"\r"])  # only the probe
        self.assertFalse(self.sess.push_lock.locked())

    def test_refused_while_push_runs(self):
        c = self._session()
        self.sess.push_lock.acquire()
        try:
            self.assertEqual(c.post("/session/reboot").status_code, 409)
        finally:
            self.sess.push_lock.release()
        self.assertEqual(self.board.sent, [])


class RebootFieldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = profiles_mod.profile_dir()
        profiles_mod.set_profile_dir(self._tmp.name)
        profiles_mod.save_profile(Profile("MKO-PROD-AGSPARE", [TS], "ppcbug", system="AGSPARE"))
        self.c = create_app().test_client()

    def tearDown(self):
        profiles_mod.set_profile_dir(self._old)
        self._tmp.cleanup()

    def _raw(self):
        return json.loads((Path(self._tmp.name) / "MKO-PROD-AGSPARE.json").read_text())

    def test_preset_dialog_saves_reboot(self):
        r = self.c.post("/profiles/MKO-PROD-AGSPARE/update",
                        data={"loader_hint": "ppcbug", "reboot": "dbpf ag:iocStats:SysReset 1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._raw()["reboot"], "dbpf ag:iocStats:SysReset 1")
        self.assertIn('name="reboot" value="dbpf ag:iocStats:SysReset 1"', r.get_data(as_text=True))

    def test_bad_reboot_is_refused_and_not_saved(self):
        r = self.c.post("/profiles/MKO-PROD-AGSPARE/update",
                        data={"loader_hint": "ppcbug", "reboot": "^1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._raw().get("reboot", ""), "")

    def test_form_without_reboot_keeps_it(self):
        self.c.post("/profiles/MKO-PROD-AGSPARE/update", data={"loader_hint": "ppcbug", "reboot": "exit"})
        self.c.post("/profiles/MKO-PROD-AGSPARE/update", data={"loader_hint": "ppcbug", "param_x": ""})
        self.assertEqual(self._raw()["reboot"], "exit")


if __name__ == "__main__":
    unittest.main()
