"""临时对话保存为 Agent（#318）：对话归到新 Agent 名下，会话、目录接得上；不碰真实主目录。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, registry


class SaveAsAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = self.home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        self.P = platform
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18978}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        self.proj = os.path.join(home, "proj")
        os.makedirs(self.proj)
        registry.write_state(h, chats={
            "w-a": {"title": "临时", "temp": {"base": "claude"}, "session_id": "s-a"},
            "w-b": {"title": "有目录", "temp": {"base": "codex", "dir": self.proj}},
            "w-c": {"title": "在干活", "temp": {"base": "claude"}, "busy": True},
        })

    def save(self, cid, **f):
        return self.P.save_agent(self.pf, None, {"name": "周报助手", "from_chat": cid, **f})

    def test_scratch_becomes_agent_dir(self):
        scratch = self.pf.chat_dir("w-a")
        r = self.save("w-a", base="codex")
        m = self.pf.meta("w-a")
        a = agents.get(r["id"])
        self.assertEqual(r["chat"], "w-a")
        self.assertEqual(m["agent"], r["id"])
        self.assertNotIn("temp", m)
        self.assertNotIn("cwd", m)                      # 和 Agent 的目录一样，不另记
        self.assertEqual(os.path.realpath(a["dir"]), os.path.realpath(scratch))
        self.assertEqual(a["base"], "claude")           # 底座跟对话走
        self.assertEqual(m["session_id"], "s-a")        # 会话编号还在，接着聊

    def test_other_dir_keeps_chat_where_it_was(self):
        other = os.path.join(self.home, "other")
        os.makedirs(other)
        r = self.save("w-b", dir=other)
        m = self.pf.meta("w-b")
        self.assertEqual(agents.get(r["id"])["base"], "codex")
        self.assertEqual(m["cwd"], self.proj)
        self.assertEqual(self.pf.chat_dir("w-b"), self.proj)

    def test_busy_or_agent_chat_refused(self):
        with self.assertRaises(self.P.Problem):
            self.save("w-c")
        r = self.save("w-a")
        with self.assertRaises(self.P.Problem):
            self.save("w-a")
        self.assertEqual(len([x for x in agents.all_agents().values() if x["name"] == "周报助手"]), 1, r)


if __name__ == "__main__":
    unittest.main()
