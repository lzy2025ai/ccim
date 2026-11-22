"""个人空间（#542）：待办、收藏、笔记按账号分开存；动态里收藏的合进收藏，取消收藏改回定时任务上。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from ccim import registry, share, space
from ccim.schedules import Schedules


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(registry, "HOME", os.path.join(self.tmp.name, ".ccim"))
        p.start()
        self.addCleanup(p.stop)

    def test_todo(self):
        a = space.todo_add(None, "  给客户发测试账号 ", {"chat": "w-1", "msg": 12.5, "title": "交付", "agent": "x", "junk": 1})["item"]
        self.assertEqual(a["text"], "给客户发测试账号")
        self.assertEqual(a["src"], {"chat": "w-1", "msg": 12.5, "title": "交付", "agent": "x"})
        b = space.todo_add(None, "自己记的")["item"]
        self.assertNotIn("src", b)
        self.assertEqual([t["id"] for t in space.view(None)["todos"]], [b["id"], a["id"]])   # 新加的在上面
        space.todo_set(None, a["id"], done=True)
        t = space.view(None)["todos"][1]
        self.assertTrue(t["done"])
        self.assertIn("done_at", t)
        space.todo_set(None, a["id"], done=False, text="改过")
        t = space.view(None)["todos"][1]
        self.assertEqual((t["done"], t["text"]), (False, "改过"))
        self.assertNotIn("done_at", t)
        with self.assertRaises(space.Problem):
            space.todo_add(None, "  ")
        with self.assertRaises(space.Problem):
            space.todo_set(None, a["id"], text="")
        space.todo_del(None, a["id"])
        self.assertEqual(len(space.view(None)["todos"]), 1)
        space.todo_restore(None, t)                # 撤销删除：放回原来的位置
        self.assertEqual([x["id"] for x in space.view(None)["todos"]], [b["id"], a["id"]])
        space.todo_restore(None, t)                # 已经在了：不重复
        self.assertEqual(len(space.view(None)["todos"]), 2)

    def test_accounts_apart(self):
        space.todo_add(None, "主人的")
        space.todo_add("u1", "访客的")
        space.note_save("../x", None, "怪名字", "")
        self.assertEqual([t["text"] for t in space.view(None)["todos"]], ["主人的"])
        self.assertEqual([t["text"] for t in space.view("u1")["todos"]], ["访客的"])
        self.assertEqual(space.view("u2")["todos"], [])
        for f in os.listdir(os.path.join(registry.HOME, "space")):
            self.assertRegex(f, r"^[A-Za-z0-9_.-]+\.json$")

    def test_fav(self):
        src = {"chat": "w-1", "msg": 100.25, "title": "方案", "agent": "x"}
        r = space.fav(None, True, src, "回答原文")
        again = space.fav(None, True, src, "回答原文")
        self.assertEqual(r["id"], again["id"])     # 同一条回答只收一次
        v = space.view(None)
        self.assertEqual(len(v["favs"]), 1)
        self.assertEqual((v["favs"][0]["kind"], v["favs"][0]["text"]), ("chat", "回答原文"))
        space.fav(None, False, src)
        self.assertEqual(space.view(None)["favs"], [])
        with self.assertRaises(space.Problem):
            space.fav(None, True, {"chat": "w-1"}, "没有指明哪条")
        r = space.fav(None, True, src, "x")
        space.fav_del(None, r["id"])
        self.assertEqual(space.view(None)["favs"], [])

    def test_feed_stars_merged(self):
        space.fav(None, True, {"chat": "w-1", "msg": "m1"}, "对话里的")
        now = time.time()
        stars = [{"sid": "a", "rid": "a:1", "name": "风向标", "text": "动态里的", "chat": "s-a", "agent": "x", "at": now + 10}]
        favs = space.view(None, stars)["favs"]
        self.assertEqual([f["kind"] for f in favs], ["feed", "chat"])   # 按时间新的在前
        self.assertEqual(favs[0]["id"], "feed:a:a:1")             # 那次运行的编号里本来就带冒号
        self.assertEqual(space.feed_id(favs[0]["id"]), ("a", "a:1"))
        self.assertEqual(space.feed_id("feed:a:b:c"), ("a", "b:c"))
        self.assertIsNone(space.feed_id("abc"))

    def test_notes(self):
        n = space.note_save(None, None, "标题", "正文")["item"]
        time.sleep(0.01)
        m = space.note_save(None, None, "", "第二篇")["item"]
        self.assertEqual([x["id"] for x in space.view(None)["notes"]], [m["id"], n["id"]])
        time.sleep(0.01)
        space.note_save(None, n["id"], "标题", "改过")
        notes = space.view(None)["notes"]
        self.assertEqual(notes[0]["id"], n["id"])  # 最近改的在上面
        self.assertEqual(notes[0]["body"], "改过")
        with self.assertRaises(space.Problem):
            space.note_save(None, "nope", "", "")
        space.note_del(None, n["id"])
        self.assertEqual([x["id"] for x in space.view(None)["notes"]], [m["id"]])

    def test_guest_allowed(self):
        self.assertIn("/api/space", share.GUEST_GET)
        for p in ("/api/space/todo/add", "/api/space/fav", "/api/space/fav/del", "/api/space/note/save"):
            self.assertIn(p, share.GUEST_POST)


class StarredTests(unittest.TestCase):
    def test_starred(self):
        logs = {"schedule-a": [{"type": "run", "id": "a:0", "state": "done", "at": 1, "done_at": 2, "text": "老的也算", "chat": "s-a"},
                               {"type": "run", "id": "a:1", "state": "done", "at": 3, "done_at": 4, "text": "没收藏", "chat": "s-a"},
                               {"type": "run", "id": "a:2", "state": "done", "at": 5, "done_at": 6, "text": " ", "chat": "s-a"}],
                "schedule-b": [{"type": "run", "id": "b:0", "state": "done", "at": 1, "text": "x"}]}

        class L:
            def read(self, k):
                return logs.get(k, [])

        class PF:
            class channel:
                pass

            def chats(self):
                return {}

        PF.channel.logs = L()
        sc = Schedules.__new__(Schedules)
        sc.pf = PF()
        sc.all = lambda: {"a": {"name": "风向标", "target": {"kind": "agent", "id": "x"}, "stars": ["a:0", "a:2"]},
                          "b": {"name": "没收藏的", "target": {"kind": "agent", "id": "y"}}}
        sc._log = lambda sid: f"schedule-{sid}"
        r = sc.starred(["a", "b", "gone"])
        self.assertEqual([(x["rid"], x["text"], x["agent"], x["at"]) for x in r], [("a:0", "老的也算", "x", 2)])


if __name__ == "__main__":
    unittest.main()
