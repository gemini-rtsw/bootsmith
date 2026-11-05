"""A push after Boot must not be typed into the running OS (2026-11-05 demo).

Before the fix the watcher stayed at_prompt after /params/boot, so a second
Push sent the vxWorks `c` dialogue into the IOC shell.
"""

from __future__ import annotations

import threading
import unittest
from collections import deque

from bootsmith.app import create_app
from bootsmith.profiles import Profile, TerminalServer
from bootsmith.session import Session
from bootsmith.watcher import BannerWatcher, STATE_AT_PROMPT, STATE_RUNNING


class FakeBoard:
    """Answers CR with the boot prompt while `at_prompt`, else the OS shell."""

    def __init__(self):
        self.at_prompt = True
        self.sent: list[bytes] = []
        self._subs: list[deque] = []
        self._lock = threading.Lock()

    def subscribe(self, seed_history=True):
        q = deque()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs = [s for s in self._subs if s is not q]

    def snapshot(self):
        return b""

    def write(self, data: bytes):
        self.sent.append(data)
        if data == b"@\r":
            self.at_prompt = False
            return
        if data == b"\r":
            reply = b"\r\n[VxWorks Boot]: " if self.at_prompt else b"\r\n-> "
            with self._lock:
                for q in self._subs:
                    q.append(reply)


class PushAfterBootTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        profile = Profile(
            name="MKO-PROD-GNIRSCC",
            system="GNIRSCC",
            loader_hint="vxworks",
            terminal_servers=[TerminalServer(name="production", host="10.2.2.86", port=10003)],
            boot_params={"host_name": "mkotcsbootv2-lv1"},
        )
        self.board = FakeBoard()
        self.watcher = BannerWatcher(self.board, profile)
        self.watcher._set_state(STATE_AT_PROMPT)
        self.sess = Session(
            profile=profile,
            terminal_server=profile.terminal_servers[0],
            transport=self.board,
            watcher=self.watcher,
        )
        self.app.config["sessions"].current = lambda sid: self.sess
        self.client = self.app.test_client()

    def test_boot_leaves_prompt_state(self):
        self.assertEqual(self.client.post("/params/boot").status_code, 200)
        self.assertIn(b"@\r", self.board.sent)
        self.assertEqual(self.watcher.status().state, STATE_RUNNING)
        self.assertFalse(self.sess.push_lock.locked())

    def test_push_after_boot_is_refused_and_sends_nothing_but_cr(self):
        self.client.post("/params/boot")
        self.board.sent.clear()
        r = self.client.post("/params/push")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not at its boot prompt", r.get_data(as_text=True))
        self.assertEqual(self.board.sent, [])  # state is running: no probe either
        self.assertFalse(self.sess.push_lock.locked())

    def test_stale_at_prompt_state_is_caught_by_the_probe(self):
        # Board rebooted into its OS behind bootsmith's back; state still says at_prompt.
        self.board.at_prompt = False
        r = self.client.post("/params/push")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.board.sent, [b"\r"])  # only the probe CR
        self.assertEqual(self.watcher.status().state, STATE_RUNNING)
        self.assertFalse(self.sess.push_lock.locked())

    def test_boot_refused_when_not_at_prompt(self):
        self.board.at_prompt = False
        r = self.client.post("/params/boot")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(b"@\r", self.board.sent)

    def test_push_lock_busy_sends_nothing(self):
        self.sess.push_lock.acquire()
        try:
            r = self.client.post("/params/push")
        finally:
            self.sess.push_lock.release()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.board.sent, [])


if __name__ == "__main__":
    unittest.main()


class HelpAndTooltipTests(unittest.TestCase):
    def test_help_page_covers_use_git_and_rollout(self):
        html = create_app().test_client().get("/help").get_data(as_text=True)
        for needle in ("Using bootsmith", "Saving presets to git", "git push",
                       "Rolling out a new version", "systemctl restart bootsmith",
                       "{{.Image}}"):
            self.assertIn(needle, html)

    def test_boot_param_labels_have_hover_help(self):
        from bootsmith import schemas
        self.assertIn("10.2.2.145", schemas.help_for("host_inet"))
        self.assertEqual(schemas.help_for("nope"), "")
