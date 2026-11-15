"""群成员用 ask_agent 把事交给群主 Agent（#488）；不跑真实对话。"""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from ccim.mentions import Mentions
from ccim.platform import Logs, Platform
from ccim.session import Task


class ToMainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        self.sub = Mentions.sub("w-a", "mem")
        self.meta = {"w-a": {"agent": "boss", "members": ["mem"], "title": "群"},
                     self.sub: {"agent": "mem", "mention_of": "w-a"}}
        self.pf = object.__new__(Platform)
        self.pf.channel = SimpleNamespace(logs=self.logs, progress_of={}, _emit=lambda cid, rec: None)
        self.pf.chats = lambda: self.meta
        self.pf.meta = lambda cid: self.meta.get(cid) or {}
        self.pf.store = SimpleNamespace(write=lambda chats: None)
        self.pf.background = set()
        self.pf.peers = SimpleNamespace(cache={})
        self.got = []
        self.pf._bridge = lambda cid: SimpleNamespace(chat=lambda c, t: SimpleNamespace(submit=self.got.append))
        self.pf.mentions = self.m = Mentions(self.pf)

    def tearDown(self):
        self.dir.cleanup()

    async def test_hand_off_logs_in_group_and_wakes_main(self):
        task = self.pf.to_main(self.sub, "去开发 ai-browser", 1)
        self.assertEqual(self.got, [task])
        self.assertEqual(task.depth, 1)
        self.assertIn("去开发 ai-browser", task)
        rec = [r for r in self.logs.read("w-a") if r.get("type") == "user"][-1]
        self.assertEqual((rec["by"], rec["to"], rec["text"]), ("mem", ["boss"], "去开发 ai-browser"))

    async def test_asked_by_only_while_waiting(self):
        task = Task("问你个事")
        self.m.tasks["u-1:mem"] = (self.sub, task)
        self.assertIsNone(self.m.asked_by(self.sub, task))       # 用户 @ 的：没人等
        self.m.asker["u-1:mem"] = "boss"
        self.assertEqual(self.m.asked_by(self.sub, task), "boss")
        self.assertIsNone(self.m.asked_by(self.sub, Task("别的")))
        self.m._settle("w-a", {"type": "mention", "id": "u-1:mem", "state": "done", "text": "好"})
        self.assertIsNone(self.m.asked_by(self.sub, task))


if __name__ == "__main__":
    unittest.main()
