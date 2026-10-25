"""上线、更新这台 Mac（#228）：只改了文档、网页这类的不重启。"""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ccim import devpanel, runtime


def git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, check=True).stdout.strip()


class NeedsRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        git(self.d, "init", "-q")
        git(self.d, "config", "user.email", "t@example.com")
        git(self.d, "config", "user.name", "t")
        p = patch.object(runtime, "SRC", self.d)
        p.start()
        self.addCleanup(p.stop)
        self.base = self.commit({"ccim/a.py": "1"})

    def commit(self, files):
        for name, text in files.items():
            path = os.path.join(self.d, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        git(self.d, "add", "-A")
        git(self.d, "commit", "-qm", "c")
        return git(self.d, "rev-parse", "HEAD")

    def test_docs_only(self):
        c = self.commit({"CLAUDE.md": "x", "docs/模块/会话.md": "x", "skills/s/SKILL.md": "x", ".github/w.yml": "x"})
        self.assertFalse(runtime.needs_restart(self.base, c))
        self.assertFalse(runtime.needs_restart(self.base[:7], c[:7]))

    def test_code(self):
        c = self.commit({"CLAUDE.md": "y", "ccim/a.py": "2"})
        self.assertTrue(runtime.needs_restart(self.base, c))

    def test_web_only_live(self):
        c = self.commit({"ccim/web.html": "<p>"})
        self.assertFalse(runtime.needs_restart(self.base, c, live=True))   # 开发版：网页每次请求现读
        self.assertTrue(runtime.needs_restart(self.base, c))               # 装好的稳定版：要换新版本

    def test_same_and_unknown(self):
        c = self.commit({"ccim/b.py": "1"})
        self.assertFalse(runtime.needs_restart(c, c[:7]))
        self.assertTrue(runtime.needs_restart(c + "~1", c))                # 引用写法不当成缩写
        self.assertTrue(runtime.needs_restart("", c))
        self.assertTrue(runtime.needs_restart("0" * 40, c))                # 找不到的提交


class UpdateTests(unittest.TestCase):
    def run_update(self, promote, restart, trying=None):
        d = devpanel.Dev.__new__(devpanel.Dev)
        d.started, d.job, d._step = "a" * 40, {}, lambda t: None
        with patch.object(runtime, "_repo", lambda: "/repo"), \
                patch.object(devpanel, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")), \
                patch.object(runtime, "_git", lambda *a: subprocess.CompletedProcess(a, 0, "b" * 40, "")), \
                patch.object(runtime, "trying", lambda: trying), \
                patch.object(runtime, "stable_behind", lambda new=None: promote), \
                patch.object(runtime, "needs_restart", lambda a, b, live=False: restart), \
                patch.object(runtime, "start_promote") as sp, patch.object(runtime, "set_try") as st:
            quiet = d._update()
        return quiet, sp.called, st.called

    def test_nothing_to_restart(self):
        self.assertEqual(self.run_update(False, False), (True, False, False))

    def test_stable_only(self):
        self.assertEqual(self.run_update(True, False), (False, True, False))

    def test_dev_restart(self):
        self.assertEqual(self.run_update(False, True), (False, False, True))

    def test_trying_switches_back(self):
        self.assertEqual(self.run_update(False, False, trying={"branch": "x", "src": "/w"}), (False, False, True))


if __name__ == "__main__":
    unittest.main()
