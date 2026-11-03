"""Target list grouping (system -> connection -> preset) and switching the
preset of an open connection."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from bootsmith import profiles as profiles_mod
from bootsmith import session as session_mod
from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer, group_by_system, system_name

from test_multi_session import FakeWatcher, _fake_connect

PROD = TerminalServer("production", "10.2.2.86", 10003)
TEST = TerminalServer("mkocmprm test vme", "10.2.4.109", 2303)


class SystemNameTests(unittest.TestCase):
    def test_names_in_bootsmith_config(self):
        for name, system in (
            ("MKO-PROD-GNIRSCC", "GNIRSCC"),
            ("MKO-PROD-GNIRSCC-new-bootserver", "GNIRSCC"),
            ("MKO-PROD-GNIRSCC-m2lab", "GNIRSCC"),
            ("MKO_PROD-OIWFS", "OIWFS"),
            ("MKO-GMOSCC-new-server", "GMOSCC"),
            ("CRCS-sim8", "CRCS"),
            ("CRCS-m2lab-sim", "CRCS"),
            ("MKO-PROD-PCS-m2-lab", "PCS"),
            ("MKO-PROD-GNIRSDC1-new-bootserver", "GNIRSDC1"),
            ("MKO-PROD", "MKO-PROD"),
        ):
            self.assertEqual(system_name(name), system, name)


class GroupingTests(unittest.TestCase):
    def test_connections_are_shared_by_presets_of_a_system(self):
        prod = Profile("MKO-PROD-GNIRSCC", [PROD, TEST])
        new = Profile("MKO-PROD-GNIRSCC-new-bootserver", [TerminalServer("prod", "10.2.2.86", 10003)])
        bare = Profile("MKO-PROD-GNIRSCC-m2lab", [])
        other = Profile("MKO-PROD-MCS", [TEST])
        systems = group_by_system([new, other, bare, prod])
        self.assertEqual([s.name for s in systems], ["GNIRSCC", "MCS"])
        gnirs = systems[0]
        # Plain production preset first.
        self.assertEqual(gnirs.presets[0].name, "MKO-PROD-GNIRSCC")
        self.assertEqual(len(gnirs.presets), 3)
        # Production port first; both presets that use it are under it,
        # each with the terminal-server name it gives the port.
        port = gnirs.connections[0]
        self.assertEqual((port.host, port.port), ("10.2.2.86", 10003))
        self.assertEqual(
            [(p.name, ts) for p, ts in port.presets],
            [("MKO-PROD-GNIRSCC", "production"), ("MKO-PROD-GNIRSCC-new-bootserver", "prod")],
        )
        self.assertEqual([p.name for p, _ in gnirs.connections[1].presets], ["MKO-PROD-GNIRSCC"])


class PresetSwitchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = profiles_mod.profile_dir()
        profiles_mod.set_profile_dir(self._tmp.name)
        for name, servers in (
            ("MKO-PROD-GNIRSCC", [PROD, TEST]),
            ("MKO-PROD-GNIRSCC-new-bootserver", [PROD]),
            ("MKO-PROD-GNIRSCC-bench", [TEST]),
        ):
            profiles_mod.save_profile(Profile(name, servers, loader_hint="vxworks"))
        for p in (
            mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(_fake_connect)),
            mock.patch.object(session_mod, "BannerWatcher", FakeWatcher),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.app = create_app()
        self.sessions = self.app.config["sessions"]
        self.a, self.b = self.app.test_client(), self.app.test_client()
        self.a.get("/")
        self.b.get("/")

    def tearDown(self):
        for sid in list(self.sessions.all()):
            self.sessions.close(sid)
        profiles_mod.set_profile_dir(self._old_dir)
        self._tmp.cleanup()

    def _open(self, client, name):
        return client.post("/session/open", data={"name": name, "terminal_server": "production"})

    def _profile(self, client):
        return client.get("/session/status").get_json()["profile"]

    def test_session_lists_presets_on_this_port_only(self):
        body = self._open(self.a, "MKO-PROD-GNIRSCC").get_data(as_text=True)
        self.assertIn("<h2>GNIRSCC</h2>", body)
        self.assertIn('value="MKO-PROD-GNIRSCC-new-bootserver"', body)
        self.assertNotIn("MKO-PROD-GNIRSCC-bench", body)

    def test_switch_preset_keeps_connection_and_other_viewer(self):
        self._open(self.a, "MKO-PROD-GNIRSCC-new-bootserver")
        self._open(self.b, "MKO-PROD-GNIRSCC-new-bootserver")
        transport = next(iter(self.sessions.all().values())).transport
        resp = self.a.post("/session/preset", data={"name": "MKO-PROD-GNIRSCC"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-profile="MKO-PROD-GNIRSCC"', resp.get_data(as_text=True))
        self.assertEqual(self._profile(self.a), "MKO-PROD-GNIRSCC")
        self.assertEqual(self._profile(self.b), "MKO-PROD-GNIRSCC-new-bootserver")
        self.assertFalse(transport.closed)
        self.assertTrue(all(s.transport is transport for s in self.sessions.all().values()))

    def test_switch_to_preset_on_another_port_is_refused(self):
        self._open(self.a, "MKO-PROD-GNIRSCC")
        resp = self.a.post("/session/preset", data={"name": "MKO-PROD-GNIRSCC-bench"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._profile(self.a), "MKO-PROD-GNIRSCC")

    def test_switch_without_session(self):
        resp = self.a.post("/session/preset", data={"name": "MKO-PROD-GNIRSCC"})
        self.assertEqual(resp.status_code, 404)

    def test_list_marks_open_connection(self):
        self._open(self.a, "MKO-PROD-GNIRSCC")
        body = self.b.get("/").get_data(as_text=True)
        self.assertIn("open in 1 tab; connecting joins it", body)


if __name__ == "__main__":
    unittest.main()
