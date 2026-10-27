"""Multiple browsers, one session each.

Each test client below has its own cookie jar, so it behaves like a
separate browser. Transport and watcher are faked; no sockets open.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from bootsmith import profiles as profiles_mod
from bootsmith import session as session_mod
from bootsmith import vxworks as vxworks_mod
from bootsmith.app import SID_COOKIE, create_app
from bootsmith.profiles import Profile, TerminalServer
from bootsmith.transport import TransportStatus

# setUp patches _connect; keep the real one for the retry-loop test.
_REAL_CONNECT = session_mod.SessionManager._connect


class FakeTransport:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.closed = False

    def status(self):
        return TransportStatus(connected=not self.closed, host=self.host, port=self.port)

    def write(self, data):
        pass

    def close(self):
        self.closed = True


class FakeWatcher:
    def __init__(self, transport, profile):
        self.profile = profile

    def start(self):
        pass

    def stop(self):
        pass

    def force_prompt(self):
        pass

    def status(self):
        return SimpleNamespace(
            state="at_prompt", loader=self.profile.loader_hint,
            abort_chars_sent=0, notes=[],
        )


def _fake_connect(ts, cancel=None):
    return FakeTransport(ts.host, ts.port)


class MultiSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = profiles_mod.profile_dir()
        profiles_mod.set_profile_dir(self._tmp.name)
        for name, port in (("GNIRSCC", 10003), ("MCS", 10004)):
            profiles_mod.save_profile(Profile(
                name=name, loader_hint="vxworks",
                terminal_servers=[TerminalServer(name="production", host="10.2.2.86", port=port)],
                boot_params={"host_name": "mkotcsbootv2-lv1"},
            ))
        patches = [
            mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(_fake_connect)),
            mock.patch.object(session_mod, "BannerWatcher", FakeWatcher),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = create_app()
        self.sessions = self.app.config["sessions"]
        self.a = self.app.test_client()
        self.b = self.app.test_client()
        self.a.get("/")
        self.b.get("/")

    def tearDown(self):
        for sid in list(self.sessions.all()):
            self.sessions.close(sid)
        profiles_mod.set_profile_dir(self._old_dir)
        self._tmp.cleanup()

    def _open(self, client, name):
        return client.post("/session/open", data={"name": name, "terminal_server": "production"})

    def _status(self, client):
        return client.get("/session/status").get_json()

    def test_two_tabs_of_one_browser_hold_separate_sessions(self):
        tab1 = {"X-Bootsmith-Tab": "tab1aaaaaaaa"}
        tab2 = {"X-Bootsmith-Tab": "tab2bbbbbbbb"}
        post = lambda h, n: self.a.post(
            "/session/open", data={"name": n, "terminal_server": "production"}, headers=h
        )
        self.assertEqual(post(tab1, "GNIRSCC").status_code, 200)
        self.assertEqual(post(tab2, "MCS").status_code, 200)
        self.assertEqual(len(self.sessions.all()), 2)
        st = lambda h: self.a.get("/session/status", headers=h).get_json()
        self.assertEqual(st(tab1)["profile"], "GNIRSCC")
        self.assertEqual(st(tab2)["profile"], "MCS")
        # The stream (EventSource can't send headers) finds its tab by ?tab=.
        self.assertEqual(self.a.get("/session/status?tab=tab2bbbbbbbb").get_json()["profile"], "MCS")
        # Same board from the other tab is refused, not stolen.
        self.assertEqual(post(tab2, "GNIRSCC").status_code, 409)
        self.assertEqual(st(tab1)["profile"], "GNIRSCC")
        # Disconnecting one tab leaves the other.
        self.a.post("/session/close", headers=tab1)
        self.assertFalse(st(tab1)["open"])
        self.assertEqual(st(tab2)["profile"], "MCS")

    def test_bad_tab_id_falls_back_to_cookie(self):
        self._open(self.a, "GNIRSCC")
        resp = self.a.get("/session/status", headers={"X-Bootsmith-Tab": "x"})
        self.assertEqual(resp.get_json()["profile"], "GNIRSCC")

    def test_first_request_sets_session_cookie(self):
        c = self.app.test_client()
        resp = c.get("/")
        self.assertIn(SID_COOKIE, resp.headers.get("Set-Cookie", ""))

    def test_two_browsers_hold_separate_sessions(self):
        self.assertEqual(self._open(self.a, "GNIRSCC").status_code, 200)
        self.assertEqual(self._open(self.b, "MCS").status_code, 200)
        self.assertEqual(self._status(self.a)["profile"], "GNIRSCC")
        self.assertEqual(self._status(self.b)["profile"], "MCS")
        self.assertEqual(len(self.sessions.all()), 2)

    def test_disconnect_only_closes_own_session(self):
        self._open(self.a, "GNIRSCC")
        self._open(self.b, "MCS")
        self.a.post("/session/close")
        self.assertFalse(self._status(self.a)["open"])
        self.assertEqual(self._status(self.b)["profile"], "MCS")

    def test_new_browser_sees_no_session(self):
        self._open(self.a, "GNIRSCC")
        self.assertFalse(self._status(self.b)["open"])

    def test_port_held_by_other_browser_is_refused(self):
        self._open(self.a, "GNIRSCC")
        resp = self._open(self.b, "GNIRSCC")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("in use by another tab or browser", resp.get_data(as_text=True))
        # A keeps its connection.
        self.assertEqual(self._status(self.a)["profile"], "GNIRSCC")

    def test_same_browser_can_reopen_its_own_port(self):
        self._open(self.a, "GNIRSCC")
        self.assertEqual(self._open(self.a, "GNIRSCC").status_code, 200)
        self.assertEqual(len(self.sessions.all()), 1)

    def test_switching_target_releases_previous_port(self):
        self._open(self.a, "GNIRSCC")
        self._open(self.a, "MCS")
        self.assertEqual(self._open(self.b, "GNIRSCC").status_code, 200)

    def test_concurrent_connect_to_same_port_only_one_wins(self):
        gate = threading.Event()

        def slow_connect(ts, cancel=None):
            gate.wait(2)
            return FakeTransport(ts.host, ts.port)

        results = {}
        with mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(slow_connect)):
            t = threading.Thread(
                target=lambda: results.setdefault("a", self._open(self.a, "GNIRSCC").status_code)
            )
            t.start()
            time.sleep(0.2)  # A is now mid-connect
            resp_b = self._open(self.b, "GNIRSCC")
            gate.set()
            t.join(3)
        self.assertEqual(results["a"], 200)
        self.assertEqual(resp_b.status_code, 409)
        self.assertIn("being connected in another tab or browser", resp_b.get_data(as_text=True))

    def test_cancel_stops_connect_and_frees_port(self):
        started = threading.Event()

        def hanging_connect(ts, cancel):
            started.set()
            if cancel.wait(5):
                raise session_mod.ConnectCancelled("Connect cancelled.")
            return FakeTransport(ts.host, ts.port)

        results = {}
        with mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(hanging_connect)):
            t = threading.Thread(target=lambda: results.setdefault("a", self._open(self.a, "GNIRSCC")))
            t.start()
            self.assertTrue(started.wait(2))
            t0 = time.time()
            self.assertEqual(self.a.post("/session/cancel").status_code, 204)
            t.join(3)
        self.assertLess(time.time() - t0, 1)
        self.assertEqual(results["a"].status_code, 409)
        self.assertIn("Connect cancelled.", results["a"].get_data(as_text=True))
        self.assertFalse(self._status(self.a)["open"])
        # The port is free for another browser straight away.
        self.assertEqual(self._open(self.b, "GNIRSCC").status_code, 200)

    def test_cancel_only_affects_own_browser(self):
        started = threading.Event()

        def hanging_connect(ts, cancel):
            started.set()
            if cancel.wait(1):
                raise session_mod.ConnectCancelled("Connect cancelled.")
            return FakeTransport(ts.host, ts.port)

        results = {}
        with mock.patch.object(session_mod.SessionManager, "_connect", staticmethod(hanging_connect)):
            t = threading.Thread(target=lambda: results.setdefault("a", self._open(self.a, "GNIRSCC")))
            t.start()
            self.assertTrue(started.wait(2))
            self.b.post("/session/cancel")
            t.join(3)
        self.assertEqual(results["a"].status_code, 200)

    def test_cancel_with_nothing_running_is_harmless(self):
        self._open(self.a, "GNIRSCC")
        self.assertEqual(self.a.post("/session/cancel").status_code, 204)
        self.assertEqual(self._status(self.a)["profile"], "GNIRSCC")

    def test_connect_retry_loop_stops_on_cancel(self):
        cancel = threading.Event()
        cancel.set()
        ts = TerminalServer(name="production", host="10.2.2.86", port=10003)
        with mock.patch.object(session_mod, "TelnetTransport") as tt:
            with self.assertRaises(session_mod.ConnectCancelled):
                _REAL_CONNECT(ts, cancel)
        tt.assert_not_called()

    def test_push_job_is_per_session(self):
        self._open(self.a, "GNIRSCC")
        self._open(self.b, "MCS")
        with mock.patch.object(
            vxworks_mod, "write_params",
            return_value=vxworks_mod.WriteResult(raw=b"", fields_written=["host_name"]),
        ), mock.patch.object(
            vxworks_mod, "read_params",
            return_value=vxworks_mod.ReadResult(params={"host_name": "mkotcsbootv2-lv1"}, raw=b""),
        ) as read_mock:
            self.assertEqual(self.a.post("/params/push").status_code, 200)
            for _ in range(100):
                if not any(s.push_lock.locked() for s in self.sessions.all().values()):
                    break
                time.sleep(0.02)
        a_sess, b_sess = (
            next(s for s in self.sessions.all().values() if s.profile.name == n)
            for n in ("GNIRSCC", "MCS")
        )
        self.assertEqual(a_sess.push_job["state"], "done")
        self.assertEqual(b_sess.push_job["state"], "idle")
        # The push read back from A's transport, not B's.
        self.assertIs(read_mock.call_args.args[0], a_sess.transport)
        self.assertIn("Current values on GNIRSCC", self.a.get("/params/push/status").get_data(as_text=True))
        self.assertNotIn("Current values", self.b.get("/params/push/status").get_data(as_text=True))

    def test_push_without_session_is_refused(self):
        resp = self.b.post("/params/push")
        self.assertEqual(resp.status_code, 404)

    def test_idle_session_is_reaped_but_not_mid_push(self):
        self._open(self.a, "GNIRSCC")
        self._open(self.b, "MCS")
        for s in self.sessions.all().values():
            s.last_seen -= session_mod.IDLE_TIMEOUT_S + 1
        busy = next(s for s in self.sessions.all().values() if s.profile.name == "MCS")
        busy.push_lock.acquire()
        try:
            reaped = self.sessions.reap_idle()
        finally:
            busy.push_lock.release()
        self.assertEqual(len(reaped), 1)
        self.assertFalse(self._status(self.a)["open"])
        self.assertEqual(self._status(self.b)["profile"], "MCS")
        # The reaped port is free again.
        self.assertEqual(self._open(self.b, "GNIRSCC").status_code, 200)

    def test_polling_keeps_session_alive(self):
        self._open(self.a, "GNIRSCC")
        sess = next(iter(self.sessions.all().values()))
        sess.last_seen -= session_mod.IDLE_TIMEOUT_S + 1
        self._status(self.a)  # the page's 2 s poll
        self.assertEqual(self.sessions.reap_idle(), [])


if __name__ == "__main__":
    unittest.main()
