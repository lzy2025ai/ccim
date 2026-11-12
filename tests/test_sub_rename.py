"""群成员的子对话改名（#409 第二轮）：Agent 列表、头像卡片里显示自己的名字；没改过的显示群名。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, registry, share


class SubRenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        from ccim.peers import Peers
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18985}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        self.owner = agents.create(name="群主")["id"]
        self.member = agents.create(name="成员")["id"]
        self.group = pf.call(pf.new_chat, agent=self.owner, title="检查群消息")
        self.sub = pf.call(pf.new_chat, agent=self.member, title="检查群消息")
        chats = pf.chats()
        chats[self.group]["members"] = [self.member]
        chats[self.sub]["mention_of"] = self.group
        pf.store.write(chats)

    def titles(self):
        from ccim import platform
        rows = platform._agent_chats(self.pf, self.member)
        recent = platform.agent_recent(self.pf, self.member)["rows"]
        return [r["title"] for r in rows if r["id"] == self.sub], [r["title"] for r in recent if r["id"] == self.sub]

    def test_renamed_sub_keeps_own_title(self):
        self.assertEqual(self.titles(), (["检查群消息"], ["检查群消息"]))
        self.pf.call(self.pf.mark_chat, self.sub, {"title": "智能流程评估"})
        self.assertEqual(self.titles(), (["智能流程评估"], ["智能流程评估"]))


if __name__ == "__main__":
    unittest.main()
