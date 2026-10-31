import asyncio
import datetime
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, commands, diary, registry, share


class DiaryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {"HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def test_parse(self):
        self.assertEqual(diary.parse("/diary 今天不错"), "今天不错")
        self.assertEqual(diary.parse("/DIARY\n第一行\n第二行"), "第一行\n第二行")
        self.assertEqual(diary.parse("/日记 好"), "好")
        self.assertEqual(diary.parse("/diary"), "")
        self.assertIsNone(diary.parse("/diaryx 不是"))
        self.assertIsNone(diary.parse("说 /diary 不算"))

    def test_add_read_search(self):
        d1 = datetime.datetime(2026, 10, 30, 9, 5)
        d2 = datetime.datetime(2026, 10, 31, 14, 32)
        diary.add("跑步 5 公里", "小助手「晨练」", d1)
        diary.add("和客户开会，定了方案", "飞书 · proj", d2)
        diary.add("晚上看书", "", d2.replace(hour=21))
        f = os.path.join(self.tmp.name, "Documents", "ccim-日记", "2026-10-31.md")
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, "## 14:32 · 飞书 · proj\n\n和客户开会，定了方案\n\n## 21:32\n\n晚上看书\n")
        today = datetime.date(2026, 10, 31)
        r = diary.read(today=today)
        self.assertIn("和客户开会", r)
        self.assertIn("晚上看书", r)
        self.assertNotIn("跑步", r)
        r = diary.read("2026-10-30", "2026-10-31", today=today)
        self.assertLess(r.index("跑步"), r.index("开会"))
        self.assertIn("2026-10-30 09:05 · 小助手「晨练」", r)
        self.assertIn("没有日记", diary.read("2026-01-01", today=today))
        self.assertIn("开会", diary.search("客户 方案"))
        self.assertNotIn("跑步", diary.search("客户"))
        self.assertEqual(diary.search("不存在的词"), "日记里没找到。")
        self.assertIn("晨练", diary.search("晨练"))   # 记在哪也能搜到
        with self.assertRaises(ValueError):
            diary.read("2026/10/31")
        with self.assertRaises(ValueError):
            diary.add("  ", "x")

    def test_hand_edited_file(self):
        root = os.path.join(self.tmp.name, "Documents", "ccim-日记")
        os.makedirs(root)
        with open(os.path.join(root, "2026-10-01.md"), "w", encoding="utf-8") as fh:
            fh.write("开头随手写的\n\n## 8:00\n早饭\n## 不是时间的标题\n还算在 8:00 里\n")
        r = diary.read("2026-10-01")
        self.assertIn("### 2026-10-01\n开头随手写的", r)
        self.assertIn("### 2026-10-01 8:00\n早饭\n## 不是时间的标题\n还算在 8:00 里", r)


class FakeChannel:
    label = "飞书"

    def __init__(self):
        self.out = []

    async def send_text(self, chat_id, text, reply_to=None):
        self.out.append(text)


class FakeBridge:
    name = "proj"

    def __init__(self):
        self.channel = FakeChannel()

    def owner(self):
        return "me"


class FakeChat:
    chat_id = "c1"
    agent = "claude"


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {"HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def test_feishu_diary(self):
        b = FakeBridge()
        self.assertTrue(asyncio.run(commands.handle(b, FakeChat(), "/diary 在飞书里记一笔", None, "me")))
        self.assertIn("已记到", b.channel.out[-1])
        self.assertIn("在飞书里记一笔", diary.read())
        self.assertIn("飞书 · proj", diary.read())
        self.assertTrue(asyncio.run(commands.handle(b, FakeChat(), "/diary 别人", None, "other")))
        self.assertEqual(b.channel.out[-1], "只有主人能记日记。")
        self.assertTrue(asyncio.run(commands.handle(b, FakeChat(), "/diary", None, "me")))
        self.assertIn("写上", b.channel.out[-1])


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = self.home = os.path.realpath(self.tmp.name)
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
        pf.external_chats = lambda *a, **k: []
        self.server = pf.serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.aid = agents.create(name="小助手")["id"]
        self.sent = []

        async def fake_send(cid, text, files=(), ctx=None, refs=None, hidden=None, mark=None):
            self.sent.append((cid, text))
        pf.send = fake_send

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def test_send_diary(self):
        st, r = self.req("POST", "/api/chat/new", {"agent": self.aid})
        cid = r["id"]
        self.pf.call(lambda: self.pf.store.write({**self.pf.chats(), cid: {**self.pf.chats()[cid], "title": "周计划"}}))
        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "/diary 今天心情不错\n第二行", "files": []})
        self.assertEqual(st, 200, r)
        self.assertTrue(r.get("diary"))
        self.assertEqual(self.sent, [])                 # 没交给助手
        log = self.pf.channel.logs.read(cid)
        self.assertEqual([x["type"] for x in log], ["diary"])
        self.assertEqual(log[0]["text"], "今天心情不错\n第二行")
        text = diary.read()
        self.assertIn("小助手「周计划」", text)
        self.assertIn("今天心情不错\n第二行", text)
        # 群聊、@ 别人的输入框里发也一样
        st, r = self.req("POST", "/api/chat/mention", {"id": cid, "text": "/diary 第二条", "to": [self.aid], "files": []})
        self.assertEqual(st, 200, r)
        self.assertIn("第二条", diary.read())
        # 空的
        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "/diary", "files": []})
        self.assertEqual(st, 400, r)
        # 普通消息照常交给助手
        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "你好", "files": []})
        self.assertEqual(st, 200, r)
        import time
        for _ in range(50):
            if self.sent:
                break
            time.sleep(0.05)
        self.assertEqual(self.sent, [(cid, "你好")])

    def test_setting(self):
        st, r = self.req("POST", "/api/diary_cards", {"on": False})
        self.assertEqual(st, 200, r)
        self.assertEqual(registry.global_config().get("diary_cards"), "off")
        st, r = self.req("POST", "/api/diary_cards", {"on": True})
        self.assertNotIn("diary_cards", registry.global_config())


if __name__ == "__main__":
    unittest.main()
