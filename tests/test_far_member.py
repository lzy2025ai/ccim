"""群聊在别的电脑上、被 @ 的成员在这台（#593）：交给它的话写明在哪台，ask_agent 转回群所在的电脑去 @。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from ccim import platform, session


def fake_peers():
    return SimpleNamespace(me={"id": "pro", "name": "Pro"},
                           machines=lambda *a, **k: [{"id": "air", "name": "Air"}, {"id": "pro", "name": "Pro"}])


class FarNoteTests(unittest.TestCase):
    def test_note_names_both_machines(self):
        pf = SimpleNamespace(peers=fake_peers())
        note = platform.Platform._far_note(pf, "air")
        self.assertIn("这个群聊在「Air」上，你在「Pro」上", note)
        self.assertIn("run_on_machine", note)
        self.assertEqual(platform.Platform._far_note(pf, "pro"), "")
        self.assertEqual(platform.Platform._far_note(pf, None), "")


class AskResultTests(unittest.TestCase):
    def test_result_kept_after_first_read(self):
        async def go():
            f = asyncio.get_running_loop().create_future()
            pf = SimpleNamespace(ask_done={}, mentions=SimpleNamespace(waiters={"r1": f}))
            self.assertEqual(platform.Platform.peer_ask_result(pf, "r1"), {"done": False})
            f.set_result({"state": "ok", "text": "清单如下"})
            r = platform.Platform.peer_ask_result(pf, "r1")
            self.assertEqual(r, {"done": True, "state": "done", "text": "清单如下"})
            self.assertNotIn("r1", pf.mentions.waiters)
            self.assertEqual(platform.Platform.peer_ask_result(pf, "r1"), r)   # 回给那边的没送到，再问还在
            with self.assertRaises(platform.Problem):
                platform.Platform.peer_ask_result(pf, "nope")
        asyncio.run(go())

    def test_endpoint_only_from_owner_peer(self):
        with self.assertRaises(platform.Problem):
            platform.owner_peer({}, lambda: 1)
        with self.assertRaises(platform.Problem):
            platform.owner_peer({"X-From-Peer": "1", "X-Account": "x"}, lambda: 1)
        self.assertEqual(platform.owner_peer({"X-From-Peer": "1"}, lambda: 1), 1)


class AskFarTests(unittest.TestCase):
    def test_round_trip(self):
        calls, polls = [], {"n": 0}

        def forward(mid, method, path, body, headers):
            b = json.loads(body)
            calls.append((mid, path, b))
            if path == "/api/peer/ask":
                return 200, "", json.dumps({"rids": [["u-1:bob", "Bob"]], "main": "Air 管理员"}).encode()
            polls["n"] += 1
            done = polls["n"] >= 2
            return 200, "", json.dumps({"done": done, "state": "done", "text": "在这" if done else ""}).encode()

        pf = SimpleNamespace(peers=SimpleNamespace(forward=forward))

        real = asyncio.sleep

        async def go():
            with mock.patch.object(session.asyncio, "sleep", lambda *_: real(0)):
                jobs = await session._ask_far(pf, "air", "w-1", "ds", ["main", "bob"], "要清单", 1)
                self.assertEqual(jobs[0], ("Air 管理员", None))
                self.assertEqual(await jobs[1][1], ("done", "在这"))
        asyncio.run(go())
        self.assertEqual(calls[0], ("air", "/api/peer/ask", {"chat": "w-1", "by": "ds", "to": ["main", "bob"],
                                                              "text": "要清单", "depth": 1}))
        self.assertEqual(calls[1][1:], ("/api/peer/ask_result", {"rid": "u-1:bob"}))

    def test_old_version(self):
        pf = SimpleNamespace(peers=SimpleNamespace(forward=lambda *a: (404, "", b"")))
        with self.assertRaises(RuntimeError) as e:
            asyncio.run(session._ask_far(pf, "air", "w-1", "ds", ["x"], "hi", 1))
        self.assertIn("版本较旧", str(e.exception))


if __name__ == "__main__":
    unittest.main()
