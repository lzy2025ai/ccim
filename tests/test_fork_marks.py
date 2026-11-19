"""分叉（#504）：原对话的详情里列出从哪条回答分出去了哪些对话，按消息编号或回答时间对上；分出来的对话不预设标题。不碰真实主目录。"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccim import agents, board, registry, share


class ForkMarkTests(unittest.TestCase):
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
        self.aid = agents.create(name="写代码的")["id"]

    def seed(self):
        at = time.time() - 600
        cid = self.pf.call(self.pf.new_chat, agent=self.aid)
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": "第一个问题", "at": at})
        logs.append(cid, {"type": "answer", "text": "一", "at": at + 10, "fork": {"sid": "s1", "at": "uuid-1"}})
        logs.append(cid, {"type": "user", "id": "u2", "text": "第二个问题", "at": at + 20})
        logs.append(cid, {"type": "answer", "text": "二", "at": at + 30, "fork": {"sid": "s1", "at": "uuid-2"}})
        chats = self.pf.chats()
        chats[cid].update(session_id="s1", title="原对话")
        self.pf.store.write(chats)
        return cid, at

    def test_forks_listed_under_their_answer(self):
        cid, at = self.seed()
        a = self.pf.fork_chat(cid, 1, at=at + 10)["id"]
        b = self.pf.fork_chat(cid, 3, at=at + 30)["id"]
        forks = self.pf.chat_detail(cid)["forks"]
        self.assertEqual([f["id"] for f in forks], [a, b])
        self.assertEqual((forks[0]["uuid"], forks[0]["row"]), ("uuid-1", at + 10))
        self.assertEqual((forks[1]["uuid"], forks[1]["row"]), ("uuid-2", at + 30))
        # 标题不照搬原对话：开口前显示从哪个问题分出，开口后由第一句话起
        self.assertFalse(self.pf.meta(a).get("title"))
        self.assertIn("第一个问题", forks[0]["title"])
        self.assertNotIn("原对话", forks[0]["title"])
        # 分出来的对话自己没有分叉
        self.assertEqual(self.pf.chat_detail(a)["forks"], [])

    def test_fork_titled_by_what_is_said_after(self):
        cid, at = self.seed()
        a = self.pf.fork_chat(cid, 1, at=at + 10)["id"]
        logs = self.pf.channel.logs
        logs.append(a, {"type": "user", "id": "u3", "text": "换个方向试试", "at": at + 100})
        logs.append(a, {"type": "answer", "text": "好", "at": at + 110})
        chats = self.pf.chats()
        chats[a]["title"] = "换个方向试试"           # 发第一句话时记的
        self.pf.store.write(chats)
        got = []

        async def fake(user, answer, first=None):
            got.append(first)
            return "进度", "换方向的尝试"
        with patch.object(board, "make_brief", fake):
            asyncio.run_coroutine_threadsafe(board.refresh_brief(self.pf, a), self.pf.loop).result(10)
        self.assertEqual(got, ["换个方向试试"])
        self.assertEqual(self.pf.meta(a)["title"], "换方向的尝试")

    def test_deleted_fork_not_listed(self):
        cid, at = self.seed()
        a = self.pf.fork_chat(cid, 1, at=at + 10)["id"]
        self.pf.run(self.pf.delete_chat(a))
        self.assertEqual(self.pf.chat_detail(cid)["forks"], [])


if __name__ == "__main__":
    unittest.main()
