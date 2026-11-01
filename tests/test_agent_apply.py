"""改了 Agent 的模型、思考深度（#387）：有聊过的对话先问；选了用到已开的对话就换上，选只用于新对话就留着原来的。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, registry


class AgentApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.object(registry, "global_config", lambda: {}),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        self.P = platform
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18979}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        d = os.path.join(home, "proj")
        os.makedirs(d)
        self.aid = agents.create(name="写手", dir=d, model="sonnet")["id"]
        registry.write_state(h, chats={
            "w-a": {"agent": self.aid, "session_id": "s-a"},
            "w-b": {"agent": self.aid, "session_id": "s-b", "model": "haiku"},     # 单独设过
            "w-c": {"agent": self.aid},                                             # 还没聊过
            "w-d": {"agent": self.aid, "session_id": "s-d", "archived": True},
        })
        self.chat = self.pf._bridge("w-a").chat("w-a", "p2p")

    def save(self, **f):
        return self.P.save_agent(self.pf, self.aid, {"model": "opus", **f})

    def test_asks_when_open_chats(self):
        self.assertEqual(self.save(), {"confirm": "apply", "count": 1})
        self.assertEqual(agents.get(self.aid)["model"], "sonnet")     # 没选之前不存
        self.assertNotIn("confirm", self.P.save_agent(self.pf, self.aid, {"role": "新的"}))   # 只改角色不问

    def test_live(self):
        self.save(apply="live")
        self.assertEqual(self.chat.model, "opus")
        self.assertTrue(self.chat.reconnect)
        self.assertEqual(self.pf._effective(self.pf.meta("w-a"), self.pf.meta("w-a"))["model"], "opus")

    def test_new_only_keeps_old(self):
        self.save(apply="new")
        self.assertEqual(self.chat.model, "sonnet")
        m = self.pf.meta("w-a")
        self.assertEqual(m["keep"], {"model": "sonnet"})
        self.assertNotIn("keep", self.pf.meta("w-b"))
        self.assertNotIn("keep", self.pf.meta("w-c"))
        self.assertEqual(self.pf._effective(m, m)["model"], "sonnet")
        b = self.pf._bridge("w-a")
        b.chats.clear()                                                # 重启后重新建对话：还是原来的
        c = b.chat("w-a", "p2p")
        self.assertEqual(c.model, "sonnet")
        self.assertEqual(b.chat("w-c", "p2p").model, "opus")           # 没聊过的算新对话
        c.follow("model")                                              # /model 默认：回到跟随 Agent
        self.assertEqual(c.model, "opus")
        self.assertFalse(self.pf.meta("w-a").get("keep"))

    def test_dir_change_keeps_old_chats_where_they_were(self):
        old = agents.cwd(agents.get(self.aid))
        other = os.path.join(os.path.dirname(old), "other")
        os.makedirs(other)
        r = self.P.save_agent(self.pf, self.aid, {"dir": other})
        self.assertEqual(r, {"confirm": "dir", "count": 2, "dir": old})
        self.P.save_agent(self.pf, self.aid, {"dir": other, "dir_ok": True})
        self.assertEqual(self.pf.chat_dir("w-a"), old)
        self.assertEqual(self.pf.chat_dir("w-d"), old)                 # 已完成的以后打开也接得上
        self.assertEqual(self.pf.chat_dir("w-c"), other)               # 没聊过的算新对话

    def test_feishu_follows_choice(self):
        from ccim.bridge import Bridge
        proj = agents.cwd(agents.get(self.aid))
        registry.put(proj, {"kind": "feishu", "agent_id": self.aid, "model": "sonnet"})
        registry.write_state(proj, chats={"oc_1": {"session_id": "f-1"}, "oc_2": {"session_id": "f-2", "effort": "low"}})
        b = Bridge(proj, dict(registry.get(proj)))
        b.reload = lambda: registry.get(proj)
        b.refresh()                                                    # 第一次只记下现在的
        c = b.chat("oc_1", "p2p")
        self.assertEqual(self.save(), {"confirm": "apply", "count": 3})   # 网页 1 个 + 飞书 2 个
        self.save(apply="new")
        b.refresh()
        self.assertEqual(c.model, "sonnet")
        self.assertEqual(b.chat("oc_2", "p2p").model, "sonnet")
        self.P.save_agent(self.pf, self.aid, {"effort": "high", "apply": "live"})
        b.refresh()
        self.assertEqual(c.effort, "high")
        self.assertTrue(c.reconnect)
        self.assertEqual(b.chat("oc_2", "p2p").effort, "low")          # 单独设过的不动


if __name__ == "__main__":
    unittest.main()
