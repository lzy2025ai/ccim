"""退回丢下的会话不在 Agent 列表里冒成「终端」对话（#434）。不跑真实对话。"""
import asyncio
import os
import sys
import tempfile
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccim import agents, registry, share


class RewindLeftTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18981}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        self.aid = agents.create(name="工程师", dir=home)["id"]
        self.cid = pf.call(pf.new_chat, agent=self.aid, title="群聊")

        async def rewind(files_at, sid, at):      # 退回到第一条之前：不再用原来的会话
            chats = pf.chats()
            chats[self.cid]["session_id"] = sid
            pf.store.write(chats)
        pf._bridge = lambda cid: SimpleNamespace(chat=lambda c, t: SimpleNamespace(rewind=rewind))
        pf._rewind_plan = lambda rows, i, base, m: (None, None, None)

    def test_rewind_hides_left_session(self):
        pf = self.pf
        chats = pf.chats()
        chats[self.cid]["session_id"] = "S-old"
        pf.store.write(chats)
        logs = pf.channel.logs
        logs.append(self.cid, {"type": "user", "id": "u1", "text": "代码在哪"})
        logs.append(self.cid, {"type": "answer", "text": "在这里", "fork": {"sid": "S-old"}})
        asyncio.run_coroutine_threadsafe(pf.rewind_chat(self.cid, 0, False), pf.loop).result(5)
        self.assertIsNone(pf.chats()[self.cid].get("session_id"))
        self.assertIn("S-old", pf.chats()[self.cid]["left_sids"])
        sess = [SimpleNamespace(session_id=x, custom_title=None, summary=None, first_prompt="t", last_modified=0)
                for x in ("S-old", "S-term")]
        fake = types.ModuleType("claude_agent_sdk")
        fake.list_sessions = lambda directory=None, limit=40: sess
        with patch.dict(sys.modules, {"claude_agent_sdk": fake}):
            ids = {r["sid"] for r in pf.call(pf.external_chats, self.aid, True)}
        self.assertNotIn("S-old", ids)
        self.assertIn("S-term", ids)


if __name__ == "__main__":
    unittest.main()
