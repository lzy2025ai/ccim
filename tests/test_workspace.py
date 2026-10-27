"""工作区（#206）：文件操作圈在对话的工作目录里，AI 这一轮改了哪些文件。"""
import os, shutil, tempfile, time, unittest
from unittest import mock

from ccim import workspace as w


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.trash = tempfile.mkdtemp()
        p = mock.patch.object(w, "TRASH", self.trash)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.trash, True)

    def test_stays_inside(self):
        with self.assertRaises(w.Problem):
            w.resolve(self.root, "../x")
        os.symlink("/etc", os.path.join(self.root, "lnk"))
        self.assertNotIn("lnk", [e["name"] for e in w.ls(self.root)["entries"]])
        with self.assertRaises(w.Problem):
            w.ls(self.root, "lnk")
        with self.assertRaises(w.Problem):
            w.trash(self.root, "")

    def test_paths_in_reply(self):   # 回复里写到的路径（#296）
        os.makedirs(os.path.join(self.root, "交付"))
        open(os.path.join(self.root, "交付", "包.zip"), "w").close()
        os.symlink("/etc/hosts", os.path.join(self.root, "lnk"))
        got = w.paths(self.root, ["交付/包.zip", os.path.join(self.root, "交付"), "没有.md", "../x", "/etc/hosts", "lnk", ""])["paths"]
        self.assertEqual(set(got), {"交付/包.zip", os.path.join(self.root, "交付")})
        self.assertEqual(got["交付/包.zip"], {"path": os.path.join(self.root, "交付", "包.zip"), "dir": False})
        self.assertTrue(got[os.path.join(self.root, "交付")]["dir"])
        with mock.patch.object(w.subprocess, "run") as run:
            w.reveal(self.root, "交付/包.zip")
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0], ["open", "-R", os.path.join(self.root, "交付", "包.zip")])
            for bad in ("没有.md", "/etc/hosts", "lnk"):
                with self.assertRaises(w.Problem):
                    w.reveal(self.root, bad)
            run.assert_called_once()

    def test_create_move_rename(self):
        f = w.create(self.root, "", "笔记.md")["path"]
        self.assertTrue(open(f).read().startswith("# 笔记"))
        self.assertTrue(w.create(self.root, "", "笔记.md")["path"].endswith("笔记 2.md"))
        d = w.create(self.root, "", "子目录", folder=True)["path"]
        moved = w.move(self.root, f, d)["path"]
        self.assertEqual(moved, os.path.join(d, "笔记.md"))
        with self.assertRaises(w.Problem):
            w.move(self.root, d, d)
        self.assertTrue(w.rename(self.root, moved, "正文.md")["path"].endswith("子目录/正文.md"))

    def test_trash_restore(self):
        f = w.create(self.root, "", "a.txt")["path"]
        r = w.trash(self.root, f)
        self.assertFalse(os.path.exists(f))
        self.assertEqual(w.restore(self.root, r["token"], r["path"])["path"], f)
        with self.assertRaises(w.Problem):
            w.restore(self.root, "/etc/hosts", f)

    def test_changed_since(self):
        old = os.path.join(self.root, "old.md")
        open(old, "w").write("x")
        os.utime(old, (time.time() - 100, time.time() - 100))
        t0 = time.time()
        edited = os.path.join(self.root, "edited.md")
        open(edited, "w").write("x")
        made = os.path.join(self.root, "new.md")
        open(made, "w").write("y")
        rows = {os.path.basename(r["path"]): r["new"] for r in w.changed_since(self.root, t0, [edited, made], None, [made])}
        self.assertEqual(rows, {"edited.md": False, "new.md": True})
        mine = w.changed_since(self.root, t0, [edited, made], lambda p, m: p == os.path.realpath(made), [made])
        self.assertEqual([os.path.basename(r["path"]) for r in mine], ["edited.md"])


if __name__ == "__main__":
    unittest.main()
