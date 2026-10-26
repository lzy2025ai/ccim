import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccim import agents, board, registry, share


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
        self.sent = []

        async def fake_send(cid, text, files=(), ctx=None, refs=None, hidden=None, mark=None):
            self.sent.append((cid, text, hidden, mark))
        pf.send = fake_send

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def seed(self, d):
        at = time.mktime(time.strptime(d + " 10:00", "%Y-%m-%d %H:%M"))
        cid = self.pf.call(self.pf.new_chat, agent=self.aid, title="写周报")
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": "帮我写这周的周报", "at": at})
        logs.append(cid, {"type": "answer", "text": "周报写好了，等你看", "at": at + 60, "done_at": at + 60})
        return cid

    def test_review_flow(self):
        d = "2026-10-20"
        self.seed(d)
        st, r = self.req("GET", f"/api/board/day?d={d}&material=1")
        self.assertEqual(st, 200, r)
        self.assertEqual([c["title"] for c in r["chats"]], ["写周报"])
        self.assertIn("我说：帮我写这周的周报", r["material"])
        self.assertIn("最后的回答：周报写好了", r["material"])

        st, r = self.req("POST", "/api/board/review", {"date": d, "agent": self.aid, "material": r["material"]})
        self.assertEqual(st, 200, r)
        cid = r["chat"]
        time.sleep(0.3)
        m = self.pf.meta(cid)
        self.assertEqual((m["review"], m["review_view"], m["agent"], m["title"]), (d, "summary", self.aid, "10月20日 复盘·工作总结"))
        self.assertEqual(self.sent[-1][1], "复盘这一天：工作总结")
        self.assertIn("帮我复盘 2026-10-20 这一天", self.sent[-1][2])
        self.assertIn("周报写好了", self.sent[-1][2])
        self.assertEqual(self.sent[-1][3], {"review": d})
        v = self.pf.chat_view(cid)
        self.assertEqual((v["review"], v["review_name"]), (d, "工作总结"))

        # 复盘对话本身不算进那天的对话
        st, r = self.req("GET", f"/api/board/day?d={d}")
        self.assertEqual(len(r["chats"]), 1)

        # 换一套视角、同一套再来一次：都是新的独立对话（#263）
        st, r = self.req("POST", "/api/board/review", {"date": d, "agent": self.aid, "material": "x", "view": "rethink"})
        c2 = r["chat"]
        st, r = self.req("POST", "/api/board/review", {"date": d, "agent": self.aid, "material": "x", "view": "rethink"})
        c3 = r["chat"]
        time.sleep(0.3)
        self.assertEqual(len({cid, c2, c3}), 3)
        self.assertEqual(self.pf.meta(c3)["title"], "10月20日 复盘·换个角度看看")
        self.assertIn("旁观者", self.sent[-1][2])
        self.assertEqual(self.sent[-1][1], "复盘这一天：换个角度看看")
        st, r = self.req("POST", "/api/board/review", {"date": d, "agent": self.aid, "material": "x", "view": "nope"})
        self.assertEqual(st, 400)

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
        # 没改的自带视角不存，以后默认的更新了能跟上
        saved = registry.global_config()["review_views"]
        self.assertEqual(saved[2], {"id": "time"})
        st, r = self.req("POST", "/api/board/review", {"date": "2026-10-21", "agent": self.aid, "material": "x", "view": new["id"]})
        time.sleep(0.3)
        self.assertTrue(self.sent[-1][2].startswith("看看 2026-10-21 我的状态"))
        st, r = self.req("POST", "/api/board/review", {"date": "2026-10-21", "agent": self.aid, "material": "x"})
        time.sleep(0.3)
        self.assertTrue(self.sent[-1][2].startswith("只写三句话：2026-10-21"))
        # 不合规的不存
        for bad in ([], [{"name": "", "prompt": "x"}], [{"name": "a", "prompt": "x"}, {"name": "a", "prompt": "y"}], [{"name": "空的"}]):
            st, r = self.req("POST", "/api/board/review_views", {"views": bad})
            self.assertEqual(st, 400, bad)

    def test_old_single_prompt(self):
        registry.set_global(review_prompt="以前改过的：{date}")
        st, r = self.req("GET", "/api/board/review_views")
        self.assertEqual(r["views"][0]["prompt"], "以前改过的：{date}")
        self.assertEqual(len(r["views"]), 5)

    def test_bad_input(self):
        st, r = self.req("POST", "/api/board/review", {"date": "昨天", "agent": self.aid})
        self.assertEqual(st, 400)
        st, r = self.req("POST", "/api/board/review", {"date": "2026-10-20", "agent": "nobody"})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
