"""已完成（#369，以前叫归档）：标完成时记进完成记录，取消就删；对话清理掉后任务面板统计和复盘照样算。不碰真实主目录。"""
import asyncio
import datetime
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccim import agents, board, registry, share


class FakeBridge:
    def __init__(self):
        self.chats, self.got = {}, []

    async def on_message(self, inc):
        self.got.append(inc)


class DoneTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18978}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        self.ext = []
        pf.external_chats = lambda *a, **k: [dict(r) for r in self.ext]
        self.server = pf.serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.aid = agents.create(name="写代码的")["id"]
        pf._bridge = lambda cid: FakeBridge()
        self.today = datetime.date.today().isoformat()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def seed(self, title, ask="帮我修一下", at=None):
        at = at or time.time() - 600
        cid = self.pf.call(self.pf.new_chat, agent=self.aid, title=title)
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": ask, "at": at})
        logs.append(cid, {"type": "answer", "text": "好了", "at": at + 60, "done_at": at + 60})
        return cid

    def mark(self, cid, changes):
        st, r = self.req("POST", "/api/chat/mark", {"id": cid, "changes": changes})
        self.assertEqual(st, 200, r)

    def test_mark_and_unmark(self):
        a = self.seed("修登录")
        b = self.seed("写周报", ask="写这周的周报")
        self.mark(a, {"archived": True, "pin": None})
        recs = board.done_records(self.pf)
        self.assertEqual(set(recs), {a})
        self.assertEqual((recs[a]["title"], recs[a]["agent"], recs[a]["source"]), ("修登录", self.aid, "网页"))

        st, r = self.req("GET", "/api/board")
        self.assertEqual((r["done"], r["done_week"]), ({self.today: 1}, 1))
        st, r = self.req("GET", f"/api/board/day?d={self.today}")
        self.assertEqual([(x["id"], x["gone"]) for x in r["done"]], [(a, False)])

        # 材料：开头列出完成了哪些，每个对话标出是否已完成
        mat = board.material(self.pf, self.today, self.today)
        self.assertIn("这段时间标为已完成的对话（1 个）", mat)
        self.assertIn("完成：修登录（写代码的）", mat)
        blocks = {p.split("\n")[0]: p for p in mat.split("\n\n")}
        self.assertIn("状态：已完成", next(v for k, v in blocks.items() if "### 修登录" in k))
        self.assertIn("状态：还没标完成", next(v for k, v in blocks.items() if "### 写周报" in k))

        # 改名后统计里用现在的标题；取消已完成就去掉这条
        self.mark(a, {"title": "修好登录"})
        st, r = self.req("GET", f"/api/board/day?d={self.today}")
        self.assertEqual(r["done"][0]["title"], "修好登录")
        self.mark(a, {"archived": None})
        self.assertEqual(board.done_records(self.pf), {})
        st, r = self.req("GET", "/api/board")
        self.assertEqual((r["done"], r["done_week"]), ({}, 0))
        # 置顶会顺手去掉已完成
        self.mark(b, {"archived": True})
        self.mark(b, {"pin": True, "archived": None})
        self.assertEqual(board.done_records(self.pf), {})

    def test_cleanup_keeps_record(self):
        a = self.seed("修登录", at=time.time() - 40 * 86400)
        self.mark(a, {"archived": True})
        chats = self.pf.chats()                      # 40 天前就标了完成
        old = time.time() - 40 * 86400
        chats[a]["archived_at"] = old
        chats[a]["last_active"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old))
        self.pf.store.write(chats)
        recs = self.pf._done_read()
        recs[a]["at"] = old
        self.pf._done_write(recs)
        self.pf.run(self.pf.cleanup_archived())
        self.assertNotIn(a, self.pf.chats())
        d = datetime.date.fromtimestamp(old).isoformat()
        st, r = self.req("GET", f"/api/board/day?d={d}")
        self.assertEqual([(x["title"], x["gone"]) for x in r["done"]], [("修登录", True)])
        st, r = self.req("GET", "/api/board")
        self.assertEqual(r["done"], {d: 1})
        mat = board.material(self.pf, d, d)
        self.assertIn("完成：修登录（写代码的），对话已清理，打不开", mat)

    def test_old_archived_and_external(self):
        # 以前归档的（没有完成记录）：当已完成，时间用 archived_at；清理时补进记录
        a = self.seed("老对话")
        chats = self.pf.chats()
        chats[a].update(archived=True, archived_at=time.time() - 3600)
        self.pf.store.write(chats)
        self.assertEqual(board.done_records(self.pf)[a]["title"], "老对话")
        self.ext = [{"id": "x:s1", "sid": "s1", "agent": self.aid, "agent_name": "写代码的", "source": "飞书",
                     "title": "飞书里聊的", "last_active": time.strftime("%Y-%m-%d %H:%M:%S"), "external": True}]
        registry.write_state(self.pf.home, marks={"x:s1": {"archived": True, "archived_at": time.time() - 60}})
        registry.set_global(archive_days="never")
        self.pf.run(self.pf.cleanup_archived())
        recs = self.pf._done_read()
        self.assertEqual(set(recs), {a, "x:s1"})
        self.assertEqual((recs["x:s1"]["title"], recs["x:s1"]["source"]), ("飞书里聊的", "飞书"))

        # 别处来的对话在网页上标、取消
        self.ext.append({"id": "x:s2", "sid": "s2", "agent": self.aid, "agent_name": "写代码的", "source": "终端",
                         "title": "终端里聊的", "external": True})
        self.mark("x:s2", {"archived": True})
        self.assertEqual(self.pf._done_read()["x:s2"]["source"], "终端")
        st, r = self.req("GET", f"/api/board/day?d={self.today}")
        self.assertIn("x:s2", [x["id"] for x in r["done"]])
        self.mark("x:s2", {"archived": None})
        self.assertNotIn("x:s2", self.pf._done_read())

    def test_guest_records(self):
        # 访客的已完成对话清理掉后，按记下的账号分：主人的统计里没有
        recs = {"gone-guest": {"at": time.time(), "title": "访客的", "agent": None, "source": "网页", "account": "g1"},
                "gone-owner": {"at": time.time(), "title": "主人的", "agent": None, "source": "网页"}}
        self.pf._done_write(recs)
        mine = board.done_records(self.pf, lambda cid: share.owner_map(self.pf).get(cid) is None, None)
        self.assertEqual(set(mine), {"gone-owner"})
        theirs = board.done_records(self.pf, lambda cid: share.owner_map(self.pf).get(cid) == "g1", "g1")
        self.assertEqual(set(theirs), {"gone-guest"})

    def test_review_tool_and_template(self):
        from ccim import session
        a = self.seed("修登录")
        self.mark(a, {"archived": True})
        cid = self.pf.call(self.pf.new_chat, agent=self.aid)
        tool = session._review_tool(SimpleNamespace(chat_id=cid), self.pf)
        text = self.pf.run(tool.handler({"start": self.today, "end": self.today}))["content"][0]["text"]
        self.assertIn("这段时间标为已完成的对话（1 个）", text)
        summary = board.VIEWS[0]["prompt"]
        self.assertIn("完成了什么", summary)
        self.assertIn("还挂着什么", summary)


if __name__ == "__main__":
    unittest.main()
