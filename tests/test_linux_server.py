"""Linux 服务器接入（#522）：systemd 常驻的单元、脱离服务的子进程、底座状态、设备页的废纸篓；不碰真实的 systemd。"""
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from ccim import bases, daemon, devices, registry


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = mock.patch.object(registry, "HOME", self.tmp)
        self.home.start()
        self.proj = os.path.join(self.tmp, "项目 %a $b")
        os.makedirs(self.proj)

    def tearDown(self):
        self.home.stop()

    def test_unit_quotes_args(self):
        with mock.patch.object(daemon, "_launch_env", return_value={"CCIM_X": 'a"b'}):
            text = daemon.unit_text(self.proj, "web", python="/opt/py 3/bin/python")
        exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        args = shlex.split(exec_line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
        self.assertEqual(args, ["/opt/py 3/bin/python", "-P", "-m", "ccim.cli", "run", self.proj,
                                "--channel", "web", "--mode", "always"])
        self.assertIn('Environment="CCIM_X=a\\"b"', text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("WorkingDirectory=" + self.proj.replace("%", "%%"), text)

    def test_unit_name_is_valid(self):
        name = daemon.unit_name(self.proj)
        self.assertRegex(name, r"^ccim-[0-9a-f]{12}\.service$")

    def test_no_systemd_falls_back_to_background(self):
        with mock.patch.object(daemon.sys, "platform", "linux"), \
             mock.patch.object(daemon, "systemd_ok", return_value=False), \
             mock.patch.object(daemon, "start_background", return_value=123) as bg:
            self.assertEqual(daemon.start_always(self.proj, "web"), 123)
        bg.assert_called_once()

    def test_linux_is_always_uses_unit(self):
        with mock.patch.object(daemon.sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp}):
            self.assertFalse(daemon.is_always(self.proj))
            os.makedirs(os.path.dirname(daemon.unit_path(self.proj)), exist_ok=True)
            open(daemon.unit_path(self.proj), "w").close()
            self.assertTrue(daemon.is_always(self.proj))


class DetachTests(unittest.TestCase):
    def test_detach_under_systemd(self):
        with mock.patch.object(daemon.sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}), \
             mock.patch.object(daemon.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.object(daemon, "systemd_ok", return_value=True):
            self.assertEqual(daemon.detach(["py", "-m", "x"])[:3], ["systemd-run", "--user", "--scope"])

    def test_detach_elsewhere_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(daemon.detach(["py", "-m", "x"]), ["py", "-m", "x"])
        with mock.patch.object(daemon.sys, "platform", "darwin"), mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
            self.assertEqual(daemon.detach(["py"]), ["py"])


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
            os.environ.pop(k, None)
        bases._cache.clear()

    def tearDown(self):
        self.env.stop()

    def status(self, claude=True, codex=None, dsh=None):
        from ccim import codex as cx, dsh as ds
        with mock.patch.object(bases, "_claude_cli", return_value="/x/claude" if claude else None), \
             mock.patch.object(cx, "find_codex", return_value=codex), \
             mock.patch.object(ds, "status", return_value=dsh or {"ready": False, "key": False}):
            return bases.status()

    def test_nothing_set_up(self):
        s = self.status(claude=False)
        self.assertEqual({k: v["need"] for k, v in s.items()}, {"claude": "install", "codex": "install", "dsh": "install"})
        self.assertFalse(any(v["ready"] for v in s.values()))

    def test_claude_installed_not_logged_in(self):
        self.assertEqual(self.status()["claude"], {"installed": True, "ready": False, "need": "login"})

    def test_claude_logged_in(self):
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "x"}}, f)
        self.assertTrue(self.status()["claude"]["ready"])

    def test_claude_api_key_in_settings(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_API_KEY": "k"}}, f)
        self.assertTrue(self.status()["claude"]["ready"])

    def test_codex_and_dsh(self):
        os.makedirs(os.path.join(self.home, ".codex"))
        with open(os.path.join(self.home, ".codex", "auth.json"), "w") as f:
            json.dump({"tokens": {"a": 1}}, f)
        s = self.status(codex="/x/codex", dsh={"ready": True, "key": False})
        self.assertTrue(s["codex"]["ready"])
        self.assertEqual(s["dsh"], {"installed": True, "ready": False, "need": "key"})


class LinuxTrashTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.p = [mock.patch.object(devices, "HOME", self.home), mock.patch.object(devices, "LINUX", True),
                  mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_trash_writes_info(self):
        f = os.path.join(self.home, "a b.txt")
        open(f, "w").close()
        pdir = tempfile.mkdtemp()
        r = devices.trash(pdir, [f])
        self.assertEqual(r["done"], [f])
        files = os.path.join(self.home, ".local", "share", "Trash", "files")
        self.assertEqual(os.listdir(files), ["a b.txt"])
        info = open(os.path.join(self.home, ".local", "share", "Trash", "info", "a b.txt.trashinfo")).read()
        self.assertIn("Path=" + f.replace(" ", "%20"), info)

    def test_keep_linux_dirs(self):
        with mock.patch.object(devices, "KEEP", devices.KEEP | {".local", ".config"}):
            for n in (".local", ".config"):
                with self.assertRaises(devices.Problem):
                    devices._removable(os.path.join(self.home, n))


if __name__ == "__main__":
    unittest.main()
