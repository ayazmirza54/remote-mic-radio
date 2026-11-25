"""Self-checks for the agent's recovery logic. Run: python test_server.py (any OS, uses --mock)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

import server


class Dummy:
    def com_init(self):
        pass


class ComThreadTest(unittest.TestCase):
    def test_hung_call_gets_a_fresh_thread(self):
        com = server.ComThread(Dummy())
        with self.assertRaises(TimeoutError):
            com.run(time.sleep, 5, timeout=0.3)
        t = time.time()
        self.assertEqual(com.run(lambda: 42, timeout=2), 42)  # not stuck behind the hung call
        self.assertLess(time.time() - t, 1)


class AgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.port = 18765
        src = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(src, "server.py")) as a, open(os.path.join(cls.dir, "server.py"), "w") as b:
            b.write(a.read())  # config.json lands in the temp dir, not the repo
        os.symlink(os.path.join(src, "static"), os.path.join(cls.dir, "static"))
        cls.p = subprocess.Popen([sys.executable, "server.py", "--mock", "--quiet", "--port", str(cls.port)],
                                 cwd=cls.dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        cls.token = json.load(open(os.path.join(cls.dir, "config.json")))["token"]

    @classmethod
    def tearDownClass(cls):
        cls.p.kill()
        cls.p.wait()

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"X-Token": self.token},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    def test_status_is_fast_and_reflects_actions(self):
        t = time.time()
        st = self.call("/api/status")
        self.assertLess(time.time() - t, 0.5)
        self.assertFalse(st["stale"])
        self.call("/api/mic", {"muted": True})
        self.assertTrue(self.call("/api/status")["all_muted"])  # cache refreshed right after the action

    def test_hotkey_refused_while_teams_ptt_held(self):
        self.assertTrue(self.call("/api/ptt", {"action": "down", "mode": "teams"})["ok"])
        self.assertFalse(self.call("/api/hotkey", {"app": "teams"})["ok"])
        self.call("/api/ptt", {"action": "up"})
        self.assertTrue(self.call("/api/hotkey", {"app": "teams"})["ok"])

    def test_discovery_answers_with_fingerprint_not_token(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(b"RMDISCOVER", ("127.0.0.1", self.port))
        reply = s.recv(128).decode()
        self.assertEqual(reply, f"RMAGENT {self.port} {server.agent_tag(self.token)}")
        self.assertNotIn(self.token, reply)


if __name__ == "__main__":
    unittest.main()
