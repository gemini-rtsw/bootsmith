"""Explicit preset system, and editing a system's presets and connections."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bootsmith import profiles as profiles_mod
from bootsmith import session as session_mod
from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer, group_by_system, system_of

from test_multi_session import FakeWatcher, _fake_connect

PROD = TerminalServer("production", "10.2.2.86", 10003)
LAB = TerminalServer("m2 lab", "10.1.5.85", 2308)
GMOS = TerminalServer("production", "10.2.4.105", 2303)


class SystemFieldTests(unittest.TestCase):
    def test_system_field_wins_over_name(self):
        p = Profile("lab-GMOSCC", [GMOS], system="GMOSCC")
        self.assertEqual([s.name for s in group_by_system([p])], ["GMOSCC"])

    def test_file_without_system_falls_back_to_name(self):
        self.assertEqual(system_of(Profile("MKO-PROD-GNIRSCC-m2lab", [LAB])), "GNIRSCC")

    def test_normalize_system(self):
        self.assertEqual(profiles_mod.normalize_system(" mcs "), "MCS")
        with self.assertRaises(ValueError):
            profiles_mod.normalize_system("GM OS")


class SystemEditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = profiles_mod.profile_dir()
        profiles_mod.set_profile_dir(self._tmp.name)
        profiles_mod.save_profile(Profile("MKO-PROD-GNIRSCC", [PROD], "vxworks", system="GNIRSCC"))
        profiles_mod.save_profile(Profile("MKO-PROD-GNIRSCC-new-bootserver", [PROD], "vxworks", system="GNIRSCC"))
        profiles_mod.save_profile(Profile("MKO-PROD-GNIRSCC-m2lab", [LAB], "vxworks", system="GNIRSCC"))
        profiles_mod.save_profile(Profile("MKO-PROD-GMOSCC", [GMOS], "vxworks", system="GMOSCC"))
        for p in (
            mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(_fake_connect)),
            mock.patch.object(session_mod, "BannerWatcher", FakeWatcher),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.app = create_app()
        self.c = self.app.test_client()
        self.c.get("/")

    def tearDown(self):
        sessions = self.app.config["sessions"]
        for sid in list(sessions.all()):
            sessions.close(sid)
        profiles_mod.set_profile_dir(self._old_dir)
        self._tmp.cleanup()

    def _ts(self, name):
        return [(t.host, t.port) for t in profiles_mod.get_profile(name).terminal_servers]

    def _raw(self, name):
        return json.loads((Path(self._tmp.name) / f"{name}.json").read_text())

    # --- systems
    def test_new_system_writes_first_preset_with_system_and_connection(self):
        r = self.c.post("/systems", data={
            "system": "mcs", "ts_name": "production", "ts_host": "10.2.4.110", "ts_port": "2301",
            "name": "MKO-PROD-MCS", "loader_hint": "vxworks", "param_target_name": "mcs",
        })
        self.assertEqual(r.status_code, 200)
        raw = self._raw("MKO-PROD-MCS")
        self.assertEqual(raw["system"], "MCS")
        self.assertEqual(raw["terminal_servers"], [{"name": "production", "host": "10.2.4.110", "port": 2301}])
        self.assertEqual(raw["boot_params"]["target_name"], "mcs")

    def test_new_system_refuses_existing_system(self):
        r = self.c.post("/systems", data={
            "system": "GMOSCC", "ts_name": "x", "ts_host": "1.2.3.4", "ts_port": "1",
            "name": "other", "loader_hint": "vxworks",
        })
        self.assertEqual(r.status_code, 409)

    # --- presets
    def test_new_preset_takes_system_from_section_not_name(self):
        r = self.c.post("/systems/GMOSCC/presets", data={
            "name": "bench-copy", "loader_hint": "vxworks", "used_on": "10.2.4.105:2303",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._raw("bench-copy")["system"], "GMOSCC")
        self.assertEqual(self._ts("bench-copy"), [("10.2.4.105", 2303)])

    def test_new_preset_cannot_use_another_systems_console(self):
        r = self.c.post("/systems/GMOSCC/presets", data={
            "name": "x", "loader_hint": "vxworks", "used_on": "10.2.2.86:10003",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(profiles_mod.get_profile("x"))

    def test_preset_used_on_sets_its_consoles(self):
        r = self.c.post("/profiles/MKO-PROD-GNIRSCC/update", data={
            "loader_hint": "vxworks", "used_on_sent": "1",
            "used_on": ["10.2.2.86:10003", "10.1.5.85:2308"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC"), [("10.2.2.86", 10003), ("10.1.5.85", 2308)])

    def test_preset_needs_one_console(self):
        r = self.c.post("/profiles/MKO-PROD-GNIRSCC/update", data={
            "loader_hint": "vxworks", "used_on_sent": "1",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC"), [("10.2.2.86", 10003)])

    def test_rename_keeps_system(self):
        self.c.post("/profiles/MKO-PROD-GNIRSCC-m2lab/update", data={
            "loader_hint": "vxworks", "new_name": "MCS-looks-like-mcs",
        })
        self.assertEqual(self._raw("MCS-looks-like-mcs")["system"], "GNIRSCC")

    def test_saving_old_file_writes_its_system(self):
        path = Path(self._tmp.name) / "MKO-PROD-PCS.json"
        path.write_text(json.dumps({"name": "MKO-PROD-PCS", "loader_hint": "vxworks",
                                    "terminal_servers": [{"name": "p", "host": "1.1.1.1", "port": 1}]}))
        self.assertEqual(system_of(profiles_mod.get_profile("MKO-PROD-PCS")), "PCS")
        self.c.post("/profiles/MKO-PROD-PCS/update", data={"loader_hint": "vxworks"})
        self.assertEqual(self._raw("MKO-PROD-PCS")["system"], "PCS")

    def test_duplicate_keeps_system(self):
        profiles_mod.save_profile(Profile("lab-thing", [GMOS], "vxworks", system="GMOSCC"))
        self.c.post("/profiles/lab-thing/duplicate")
        self.assertEqual(self._raw("lab-thing-copy")["system"], "GMOSCC")

    # --- connections
    def test_edit_connection_rewrites_every_preset_using_it(self):
        r = self.c.post("/systems/GNIRSCC/connections/update", data={
            "old_host": "10.2.2.86", "old_port": "10003",
            "conn_name": "production", "conn_host": "10.2.2.86", "conn_port": "10005",
            "used_by": ["MKO-PROD-GNIRSCC", "MKO-PROD-GNIRSCC-new-bootserver"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC"), [("10.2.2.86", 10005)])
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC-new-bootserver"), [("10.2.2.86", 10005)])
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC-m2lab"), [("10.1.5.85", 2308)])
        self.assertEqual(self._ts("MKO-PROD-GMOSCC"), [("10.2.4.105", 2303)])

    def test_connection_change_that_empties_a_preset_writes_nothing(self):
        r = self.c.post("/systems/GNIRSCC/connections/update", data={
            "old_host": "10.2.2.86", "old_port": "10003",
            "conn_name": "production", "conn_host": "10.2.2.99", "conn_port": "10003",
            "used_by": ["MKO-PROD-GNIRSCC"],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("new-bootserver", r.get_data(as_text=True))
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC"), [("10.2.2.86", 10003)])

    def test_new_connection_is_added_to_ticked_presets_only(self):
        r = self.c.post("/systems/GNIRSCC/connections", data={
            "conn_name": "test", "conn_host": "10.2.4.109", "conn_port": "2303",
            "used_by": ["MKO-PROD-GNIRSCC-m2lab"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC-m2lab"), [("10.1.5.85", 2308), ("10.2.4.109", 2303)])
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC"), [("10.2.2.86", 10003)])

    def test_new_connection_needs_a_preset_and_a_free_port(self):
        r = self.c.post("/systems/GNIRSCC/connections", data={
            "conn_name": "t", "conn_host": "10.2.4.109", "conn_port": "2303"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/systems/GNIRSCC/connections", data={
            "conn_name": "dup", "conn_host": "10.1.5.85", "conn_port": "2308",
            "used_by": ["MKO-PROD-GNIRSCC"]})
        self.assertEqual(r.status_code, 400)

    def test_connection_cannot_tick_another_systems_preset(self):
        r = self.c.post("/systems/GNIRSCC/connections", data={
            "conn_name": "t", "conn_host": "10.2.4.109", "conn_port": "2303",
            "used_by": ["MKO-PROD-GMOSCC"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._ts("MKO-PROD-GMOSCC"), [("10.2.4.105", 2303)])

    def test_delete_connection(self):
        r = self.c.post("/systems/GNIRSCC/connections/delete",
                        data={"old_host": "10.1.5.85", "old_port": "2308"})
        self.assertEqual(r.status_code, 400)  # m2lab would have none left
        profiles_mod.save_profile(Profile("MKO-PROD-GNIRSCC-m2lab", [LAB, PROD], "vxworks", system="GNIRSCC"))
        r = self.c.post("/systems/GNIRSCC/connections/delete",
                        data={"old_host": "10.1.5.85", "old_port": "2308"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ts("MKO-PROD-GNIRSCC-m2lab"), [("10.2.2.86", 10003)])

    # --- sessions follow the field
    def test_session_switch_refuses_other_system_on_same_port(self):
        profiles_mod.save_profile(Profile("MKO-PROD-GNIRSCC-sneaky", [PROD], "vxworks", system="MCS"))
        self.c.post("/session/open", data={"name": "MKO-PROD-GNIRSCC", "terminal_server": "production"})
        r = self.c.post("/session/preset", data={"name": "MKO-PROD-GNIRSCC-sneaky"})
        self.assertEqual(r.status_code, 400)
        body = self.c.post("/session/preset", data={"name": "MKO-PROD-GNIRSCC"}).get_data(as_text=True)
        self.assertNotIn("sneaky", body)


if __name__ == "__main__":
    unittest.main()
