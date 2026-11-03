"""任务面板：群成员子对话里直接聊的那一轮也带在整体状态里（#417）；群里 @ 它的那一轮不带。不跑真实对话。"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccim import agents, registry, share


class DirectSubBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        ps = [patch.object(registry, "HOME", os.path.join(home, ".ccim")),
              patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
              patch.dict(os.environ, {"HOME": home})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        from ccim.peers import Peers
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18979}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        owner = agents.create(name="主助手")["id"]
        member = agents.create(name="成员助手")["id"]
        self.group = pf.call(pf.new_chat, agent=owner, title="群聊")
        self.sub = pf.call(pf.new_chat, agent=member, title="群聊")
        chats = pf.chats()
        chats[self.group]["members"] = [member]
        chats[self.sub]["mention_of"] = self.group
        pf.store.write(chats)

    def head_ids(self):
        rows, _ = self.pf.call(self.pf.chat_head)
        return {r["id"]: r for r in rows}

    def set_busy(self, busy):
        chats = self.pf.chats()
        chats[self.sub]["busy"] = busy
        self.pf.store.write(chats)

    def test_direct_turn_busy_then_answered(self):
        logs = self.pf.channel.logs
        logs.append(self.sub, {"type": "user", "id": "u1", "text": "重开了", "at": time.time()})
        self.set_busy(True)
        row = self.head_ids().get(self.sub)
        self.assertIsNotNone(row)
        self.assertTrue(row["direct"] and row["busy"])
        self.assertEqual(row["ask"], "重开了")
        self.set_busy(False)
        logs.append(self.sub, {"type": "answer", "text": "测好了", "at": time.time() + 1})
        self.assertEqual(self.head_ids()[self.sub]["attention"], "done")
        chats = self.pf.chats()
        chats[self.sub]["seen_at"] = time.time() + 5
        self.pf.store.write(chats)
        self.assertNotIn(self.sub, self.head_ids())

    def test_group_turn_not_listed(self):
        self.pf.channel.logs.append(self.sub, {"type": "user", "id": "u1", "text": "@成员助手 看看", "from_chat": self.group})
        self.set_busy(True)
        self.assertNotIn(self.sub, self.head_ids())


if __name__ == "__main__":
    unittest.main()
