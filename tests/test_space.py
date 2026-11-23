"""个人空间（#542）：待办、笔记按账号分开存；对话、动态加到待办（#554）。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from ccim import registry, share, space


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

    def test_chat_todo(self):
        src = {"title": "接口联调", "agent": "x", "machine": "m1", "msg": 3}
        a = space.todo_chat(None, "w-1", True, src)["item"]
        self.assertEqual(a["text"], "接口联调")
        self.assertEqual(a["src"], {"chat": "w-1", "title": "接口联调", "agent": "x", "machine": "m1"})   # 整个对话，不带哪条回答
        self.assertEqual(space.todo_chat(None, "w-1", True, src)["item"]["id"], a["id"])   # 已经有了不再加
        m = space.todo_add(None, "回答里的", {"chat": "w-1", "msg": 9})["item"]
        other = space.todo_chat(None, "w-2", True, {})["item"]
        self.assertEqual(other["text"], "对话")
        r = space.todo_chat(None, "w-1", False)
        self.assertEqual(sorted(r["ids"]), sorted([a["id"], m["id"]]))   # 这个对话名下没做完的全部勾掉
        todos = {t["id"]: t for t in space.view(None)["todos"]}
        self.assertTrue(todos[a["id"]]["done"] and todos[m["id"]]["done"])
        self.assertFalse(todos[other["id"]]["done"])
        self.assertEqual(space.todo_chat(None, "w-1", False)["ids"], [])
        b = space.todo_chat(None, "w-1", True, src)["item"]   # 都做完了再加：新的一条
        self.assertNotEqual(b["id"], a["id"])
        with self.assertRaises(space.Problem):
            space.todo_chat(None, "", True)

    def test_feed_todo(self):
        t = space.todo_add(None, "看一下", {"feed": "a", "run": "a:1", "name": "风向标", "agent": "x"})["item"]
        self.assertEqual(t["src"], {"feed": "a", "run": "a:1", "name": "风向标", "agent": "x"})
        t = space.todo_add(None, "缺了运行", {"feed": "a"})["item"]
        self.assertNotIn("src", t)

    def test_no_favs(self):
        os.makedirs(os.path.dirname(space._file(None)))
        with open(space._file(None), "w", encoding="utf-8") as f:
            f.write('{"favs": [{"id": "1", "text": "老收藏"}]}')
        self.assertNotIn("favs", space.view(None))
        space.todo_add(None, "x")
        self.assertEqual(space._read(None)["favs"], [{"id": "1", "text": "老收藏"}])   # 老数据不删

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
        for p in ("/api/space/todo/add", "/api/space/todo/chat", "/api/space/note/save"):
            self.assertIn(p, share.GUEST_POST)


if __name__ == "__main__":
    unittest.main()
