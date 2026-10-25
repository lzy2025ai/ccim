"""ccim ship（#230）：对话里上线 ccim 的需求；对话里直接 gh pr merge ccim 的仓库会被拦下。"""
import asyncio
import contextlib
import io
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from ccim import cli, devpanel, runtime, session


class ShipTests(unittest.TestCase):
    def run_ship(self, check, jobs):
        calls, it = [], iter(jobs)

        def api(path, body=None):
            calls.append(path)
            if path == "/api/dev/check":
                return check
            if path == "/api/dev/ship":
                return {"ok": True}
            j = next(it)
            return {"job": dict(j, at=time.time())}
        out = io.StringIO()
        with patch.object(cli, "_platform_api", api), patch("time.sleep", lambda s: None), contextlib.redirect_stdout(out):
            try:
                cli.cmd_ship(types.SimpleNamespace(prs=["#229", "227"]))
                err = None
            except SystemExit as e:
                err = str(e)
        return calls, out.getvalue(), err

    def test_problem_stops_before_ship(self):
        check = {"items": [{"pr": 229, "issue": 228, "title": "a", "problem": "检查没通过"},
                           {"pr": 227, "issue": 226, "title": "b", "problem": None}]}
        calls, out, err = self.run_ship(check, [])
        self.assertNotIn("/api/dev/ship", calls)
        self.assertIn("检查没通过", out)
        self.assertIn("一个都没动", err)

    def test_ship_waits_for_result(self):
        check = {"items": [{"pr": 227, "issue": 226, "title": "b", "problem": None}]}
        jobs = [{"kind": "ship", "state": "running", "step": "#226 合并"},
                {"kind": "ship", "state": "done", "note": "#226 已上线"}]
        calls, out, err = self.run_ship(check, jobs)
        self.assertIsNone(err)
        self.assertIn("/api/dev/ship", calls)
        self.assertIn("#226 合并", out)
        self.assertIn("#226 已上线", out)

    def test_ship_failure(self):
        check = {"items": [{"pr": 227, "issue": 226, "title": "b", "problem": None}]}
        calls, out, err = self.run_ship(check, [{"kind": "ship", "state": "failed", "error": "合并失败"}])
        self.assertEqual(err, "合并失败")


class MergeHookTests(unittest.TestCase):
    def setUp(self):
        self.c = session.Chat.__new__(session.Chat)
        self.c.b = types.SimpleNamespace(path="/tmp")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.other = self.tmp.name
        subprocess.run(["git", "init", "-q", self.other], check=True)
        subprocess.run(["git", "-C", self.other, "remote", "add", "origin", "https://github.com/someone/other.git"], check=True)
        for p in (patch.object(runtime, "_is_dev_install", lambda: True), patch.object(devpanel, "_repo", lambda: "owner/ccim")):
            p.start()
            self.addCleanup(p.stop)

    def denied(self, cmd, cwd):
        return bool(asyncio.run(self.c._pre_bash({"tool_input": {"command": cmd}, "cwd": cwd}, None, None)))

    def test_repo_flag(self):
        self.assertTrue(self.denied("gh pr merge 1 --squash --repo owner/ccim", "/tmp"))
        self.assertFalse(self.denied("gh pr merge 1 -R someone/other", "/tmp"))

    def test_by_cwd(self):
        with patch.object(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "git@github.com:owner/ccim.git\n", "")):
            self.assertTrue(self.denied("gh pr merge 1 --squash", "/x"))
        self.assertFalse(self.denied("gh pr merge 1 --squash", self.other))

    def test_other_commands(self):
        self.assertFalse(self.denied("gh pr view 1 --repo owner/ccim", "/tmp"))

    def test_not_dev(self):
        with patch.object(runtime, "_is_dev_install", lambda: False):
            self.assertFalse(self.denied("gh pr merge 1 --repo owner/ccim", "/tmp"))


if __name__ == "__main__":
    unittest.main()
