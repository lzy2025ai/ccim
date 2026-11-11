"""Agent 接了飞书、微信后能主动通知主人（#463）：发到主人的私聊，微信开头带 Agent 名字；只有平台上主人的私聊给这个工具。"""
import asyncio
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import agents, cli, registry, runtime, session


class NotifyUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        self.aid = agents.create(name="商单助手")["id"]
        self.proj = os.path.join(home, "p")
        os.makedirs(self.proj)
        self.hub = os.path.join(home, "wx")
        registry.put(self.hub, {"kind": "weixin", "owner_open_id": "wx_owner", "projects": {self.proj: {"agent_id": self.aid}}})
        registry.put(self.proj, {"channel": "feishu", "agent_id": self.aid, "owner_open_id": ""})

    def test_notify_goes_to_owner(self):
        res = agents.notify_owner(self.aid, "客户有新消息")
        self.assertEqual([n for n, _ in res], ["微信", "飞书"])
        self.assertEqual(dict(res)["飞书"], "no_owner")      # 飞书还没认主人：不发
        self.assertEqual(runtime.take_notes(self.proj), [])
        notes = runtime.take_notes(self.hub)
        self.assertEqual(notes, [(runtime.OWNER, "【商单助手】客户有新消息")])
        sent = []

        class Ch:
            async def send_to_user(self, u, t): sent.append(("user", u, t))
            async def send_text(self, c, t): sent.append(("chat", c, t))
        asyncio.run(cli._note(Ch(), notes[0][0], notes[0][1], logging.getLogger("t"), self.hub))
        asyncio.run(cli._note(Ch(), "oc_1", "回报", logging.getLogger("t"), self.hub))
        self.assertEqual(sent, [("user", "wx_owner", "【商单助手】客户有新消息"), ("chat", "oc_1", "回报")])

    def test_only_owner_p2p_on_platform(self):
        class PF:
            def chats(self): return {"c1": {}}

        class B:
            entry = {"agent_id": self.aid}
            channel = type("C", (), {"platform": PF()})()

        class FakeChat:
            b, chat_id, chat_type = B(), "c1", "p2p"
            def fenced(self): return False
            def _guest(self): return False
        c = FakeChat()
        self.assertEqual(session._notify_kinds(c), ["weixin", "feishu"])
        self.assertIn("notify_user", session.notify_note(["weixin"]))
        out = asyncio.run(session._notify_user(c).handler({"text": "测试", "channel": "weixin"}))
        self.assertIn("微信", out["content"][0]["text"])
        c.chat_type = "group"
        self.assertEqual(session._notify_kinds(c), [])
        c.chat_type, B.entry = "p2p", {"agent_id": self.aid, "lent": True}
        self.assertEqual(session._notify_kinds(c), [])


if __name__ == "__main__":
    unittest.main()
