"""较早的终端对话（不在列表最近 15 个里）用链接也能打开（#367）；不碰真实主目录。"""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccim import registry

SID = "0550db8c-eab3-4d5b-b5be-5e6a2c6910f6"


class OldTerminalTests(unittest.TestCase):
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
        self.P = platform
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        self.pf = platform.Platform(h, {"kind": "platform", "channel": "web", "port": 18977})
        self.dir = os.path.join(home, "proj")
        all_agents = {"a1": {"name": "甲", "base": "codex", "dir": "/x"}, "a2": {"name": "乙", "dir": self.dir}}
        self.info = SimpleNamespace(session_id=SID, custom_title="早先的对话", summary=None, first_prompt="你好",
                                    last_modified=time.time() * 1000)
        self.found = {self.dir: self.info}
        for p in (patch.object(platform.agents, "all_agents", lambda: all_agents),
                  patch.object(platform.agents, "cwd", lambda a: a["dir"]),
                  patch.object(platform.Platform, "external_chats", lambda self, aid=None, terminal=False: []),
                  patch("claude_agent_sdk.get_session_info", lambda sid, directory=None:
                        self.found.get(directory) if sid == SID else None)):
            p.start()
            self.addCleanup(p.stop)

    def test_found_in_agent_dir(self):
        r = self.pf._external("x:" + SID)
        self.assertEqual((r["agent"], r["source"], r["title"], r["base"]), ("a2", "终端", "早先的对话", "claude"))

    def test_not_found(self):
        self.found = {}
        with self.assertRaises(self.P.Problem):
            self.pf._external("x:" + SID)
        with self.assertRaises(self.P.Problem):
            self.pf._external("x:../" + SID)

    def test_hidden_or_continued_in_web(self):
        registry.write_state(self.pf.home, marks={"x:" + SID: {"hidden": True}})
        with self.assertRaises(self.P.Problem):
            self.pf._external("x:" + SID)
        registry.write_state(self.pf.home, marks={})
        with patch.object(self.P.Platform, "chats", lambda self: {"w1": {"session_id": SID}}):
            with self.assertRaises(self.P.Problem):
                self.pf._external("x:" + SID)


if __name__ == "__main__":
    unittest.main()
