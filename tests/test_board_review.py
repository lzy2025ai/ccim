import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccim import agents, board, registry, search, share


class FakeBridge:
    def __init__(self):
        self.chats, self.got = {}, []

    async def on_message(self, inc):
        self.got.append(inc)


class ReviewTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18977}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        pf.external_chats = lambda *a, **k: []
        self.server = pf.serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.aid = agents.create(name="复盘助手")["id"]
        self.bridge = FakeBridge()
        pf._bridge = lambda cid: self.bridge

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def seed(self, d, title="写周报", ask="帮我写这周的周报", answer="周报写好了，等你看", agent=None):
        at = time.mktime(time.strptime(d + " 10:00", "%Y-%m-%d %H:%M"))
        cid = self.pf.call(self.pf.new_chat, agent=agent or self.aid, title=title)
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": ask, "at": at})
        logs.append(cid, {"type": "answer", "text": answer, "at": at + 60, "done_at": at + 60})
        return cid

    def test_review_command(self):
        cid = self.pf.call(self.pf.new_chat, agent=self.aid)
        self.pf.run(self.pf.send(cid, "/review 复盘这一周的工作任务"))
        m = self.pf.meta(cid)
        self.assertIs(m["review"], True)
        self.assertEqual(m["title"], "复盘这一周的工作任务")
        # 记录里是用户的原话，交给助手的去掉命令、接上说明（Claude Code 自己的 /review 是审代码）
        rec = [r for r in self.pf.channel.logs.read(cid) if r.get("type") == "user"][-1]
        self.assertEqual(rec["text"], "/review 复盘这一周的工作任务")
        text = self.bridge.got[-1].text
        self.assertTrue(text.startswith("复盘这一周的工作任务"), text)
        self.assertIn("review_material", text)
        self.assertIn(time.strftime("%Y-%m-%d"), text)
        v = self.pf.chat_view(cid)
        self.assertEqual((v["review"], v["review_span"]), (True, None))

        # 已经有内容的对话里发 /review：照样复盘，但不把这个对话改成复盘对话
        other = self.seed("2026-10-20")
        self.pf.run(self.pf.send(other, "/review"))
        self.assertFalse(self.pf.meta(other).get("review"))
        self.assertTrue(self.bridge.got[-1].text.startswith("复盘今天的工作。"))
        # /reviewer 不是复盘命令
        self.pf.run(self.pf.send(other, "/reviewer 看看"))
        self.assertEqual(self.bridge.got[-1].text, "/reviewer 看看")

    def test_material_range(self):
        a = self.seed("2026-10-20")
        b = self.seed("2026-10-22", title="修登录", ask="登录页报错了", answer="修好了")
        self.seed("2026-10-25", title="别的事")
        st, r = self.req("GET", "/api/board/material?from=2026-10-19&to=2026-10-23")
        self.assertEqual(st, 200, r)
        mat = r["material"]
        self.assertIn("### 写周报", mat)
        self.assertIn("我说：登录页报错了", mat)
        self.assertIn("最后的回答：修好了", mat)
        self.assertNotIn("别的事", mat)
        self.assertNotIn("open_chat", mat)               # 别的电脑来拉的：打不开，不给编号
        self.assertLess(mat.index("写周报"), mat.index("修登录"))   # 按时间先后
        local = board.material(self.pf, "2026-10-20", "2026-10-20")
        self.assertIn(f"open_chat id={a}", local)
        self.assertNotIn(b, local)
        st, r = self.req("GET", "/api/board/material?from=2026-01-01&to=2026-12-31")
        self.assertEqual(st, 400)
        st, r = self.req("GET", "/api/board/material?from=yesterday")
        self.assertEqual(st, 400)

    def test_tool_and_day_listing(self):
        from ccim import session
        self.seed("2026-10-20")
        self.seed("2026-10-22", title="修登录")
        cid = self.pf.call(self.pf.new_chat, agent=self.aid)
        self.pf.run(self.pf.send(cid, "/review 复盘上周"))
        chat = SimpleNamespace(chat_id=cid)
        tool = session._review_tool(chat, self.pf)
        out = self.pf.run(tool.handler({"start": "2026-10-22", "end": "2026-10-19"}))
        text = out["content"][0]["text"]
        self.assertTrue(text.startswith("2026-10-19 到 2026-10-22 的对话材料"), text)
        self.assertIn("写周报", text)
        self.assertIn("修登录", text)
        self.assertEqual(self.pf.meta(cid)["review"], ["2026-10-19", "2026-10-22"])
        self.pf.run(tool.handler({"start": "2026-10-24", "end": "2026-10-24"}))
        self.assertEqual(self.pf.meta(cid)["review"], ["2026-10-19", "2026-10-24"])
        out = self.pf.run(tool.handler({"start": "x", "end": "y"}))
        self.assertTrue(out.get("is_error"))

        # 以前按天的老复盘
        old = self.pf.call(self.pf.new_chat, agent=self.aid, title="10月20日 复盘·工作总结")
        chats = self.pf.chats()
        chats[old].update(review="2026-10-20", review_view="summary", review_name="工作总结")
        self.pf.store.write(chats)

        st, r = self.req("GET", "/api/board/day?d=2026-10-20")
        self.assertEqual({c["id"] for c in r["reviews"]}, {cid, old})
        self.assertEqual([c["title"] for c in r["chats"]], ["写周报"])     # 复盘对话不算进那天的对话
        st, r = self.req("GET", "/api/board/day?d=2026-10-23")
        self.assertEqual([c["id"] for c in r["reviews"]], [cid])
        self.assertEqual(r["reviews"][0]["review_span"], ["2026-10-19", "2026-10-24"])
        st, r = self.req("GET", "/api/board/day?d=2026-10-25")
        self.assertEqual(r["reviews"], [])
        # 复盘对话不进材料
        self.assertNotIn("复盘上周", board.material(self.pf, "2026-10-01", "2026-10-31"))

    def test_open_any_chat(self):
        other = agents.create(name="写代码的")["id"]
        cid = self.seed("2026-10-20", title="修登录", ask="登录页报错了", agent=other)
        text = search.open_chat(self.pf, self.aid, None, cid)
        self.assertIn("登录页报错了", text)
        self.assertIsNone(search.open_chat(self.pf, self.aid, None, "nope"))

    def test_views(self):
        st, r = self.req("GET", "/api/board/review_views")
        self.assertEqual([v["id"] for v in r["views"]], ["summary", "rethink", "time", "repeat", "agent"])
        self.assertTrue(all("{date}" in v["prompt"] and v["builtin"] for v in r["views"]))
        views = r["views"]
        views[0]["prompt"] = "只写三句话：{date}"
        views[1]["name"] = "旁观者"
        views.append({"name": "情绪", "prompt": "看看 {date} 我的状态"})
        st, r = self.req("POST", "/api/board/review_views", {"views": views})
        self.assertEqual(st, 200, r)
        got = {v["name"]: v for v in r["views"]}
        self.assertEqual(got["工作总结"]["prompt"], "只写三句话：{date}")
        self.assertEqual(got["旁观者"]["prompt"], board.VIEWS[1]["prompt"])
        new = got["情绪"]
        self.assertTrue(new["id"].startswith("v-") and not new["builtin"])
        # 没改的自带模板不存，以后默认的更新了能跟上
        saved = registry.global_config()["review_views"]
        self.assertEqual(saved[2], {"id": "time"})
        # 不合规的不存
        for bad in ([], [{"name": "", "prompt": "x"}], [{"name": "a", "prompt": "x"}, {"name": "a", "prompt": "y"}], [{"name": "空的"}]):
            st, r = self.req("POST", "/api/board/review_views", {"views": bad})
            self.assertEqual(st, 400, bad)

    def test_old_single_prompt(self):
        registry.set_global(review_prompt="以前改过的：{date}")
        st, r = self.req("GET", "/api/board/review_views")
        self.assertEqual(r["views"][0]["prompt"], "以前改过的：{date}")
        self.assertEqual(len(r["views"]), 5)


if __name__ == "__main__":
    unittest.main()
