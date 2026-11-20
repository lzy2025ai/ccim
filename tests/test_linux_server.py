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



class AdminTests(unittest.IsolatedAsyncioTestCase):
    """Linux 上的 run_as_admin：卡片同意才用 sudo -n 执行，拒绝不执行，没有免密 sudo 说清楚。"""

    def chat(self, allow):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        asked = []

        async def ask(chat_id, name, inp, ctx):
            asked.append((name, inp))
            return PermissionResultAllow() if allow else PermissionResultDeny(message="no")
        from types import SimpleNamespace
        return SimpleNamespace(chat_id="c1", b=SimpleNamespace(approvals=SimpleNamespace(ask=ask))), asked

    async def test_allow_runs_sudo_n(self):
        from ccim import admin
        chat, asked = self.chat(True)
        calls = []

        async def fake_sudo(cmd):
            calls.append(cmd)
            return True, "退出码 0\nok"
        with mock.patch.object(admin.sys, "platform", "linux"), mock.patch.object(admin, "_sudo_n", fake_sudo):
            ok, text = await admin.run(chat, "apt-get update", "装依赖")
        self.assertTrue(ok)
        self.assertEqual(calls, ["apt-get update"])
        self.assertEqual(asked, [("run_as_admin", {"command": "apt-get update", "reason": "装依赖"})])

    async def test_deny_does_not_run(self):
        from ccim import admin
        chat, _ = self.chat(False)
        with mock.patch.object(admin.sys, "platform", "linux"), \
             mock.patch.object(admin, "_sudo_n", side_effect=AssertionError("不该执行")):
            ok, text = await admin.run(chat, "rm -rf /tmp/x", "清理")
        self.assertFalse(ok)
        self.assertIn("没有执行", text)

    async def test_sudo_n_without_nopasswd(self):
        from ccim import admin

        class P:
            returncode = 1
            async def communicate(self):
                return b"", b"sudo: a password is required\n"
        seen = []

        async def fake_exec(*args, **kw):
            seen.append(args)
            return P()
        with mock.patch.object(admin.shutil, "which", return_value="/usr/bin/sudo"), \
             mock.patch.object(admin.asyncio, "create_subprocess_exec", fake_exec):
            ok, text = await admin._sudo_n("id")
        self.assertFalse(ok)
        self.assertEqual(seen[0][:3], ("/usr/bin/sudo", "-n", "--"))
        self.assertIn("NOPASSWD", text)

    def test_card(self):
        from ccim.approval import Pending
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            async def mk():
                return Pending("c", "run_as_admin", {"command": "systemctl restart nginx", "reason": "重启"}, None)
            p = loop.run_until_complete(mk())
        finally:
            loop.close()
        card = p.card("项目")
        self.assertIn("systemctl restart nginx", card["body"])
        self.assertEqual([b["text"] for b in card["buttons"]], ["执行", "不执行"])


class SudoBlockTests(unittest.IsolatedAsyncioTestCase):
    def test_pattern(self):
        from ccim.session import SUDO_CMD
        for c in ["sudo ls", "cd /x && sudo rm a", "echo; su - root", 'bash -c "sudo id"', "$(sudo cat f)",
                  "FOO=1 sudo x", "env A=b sudo x", "/usr/bin/sudo id", "pkexec x", "doas x", "x | sudo tee f"]:
            self.assertTrue(SUDO_CMD.search(c), c)
        for c in ["ls sudo-notes", "grep sudo /etc/group", "echo pseudo", "cat /etc/sudoers.d/x", "ls ~/su"]:
            self.assertFalse(SUDO_CMD.search(c), c)

    async def test_pre_bash_denies_on_linux(self):
        from ccim.session import Chat
        c = object.__new__(Chat)
        from types import SimpleNamespace
        c.b = SimpleNamespace(path="/tmp")
        data = {"tool_name": "Bash", "tool_input": {"command": "sudo apt-get install -y jq"}}
        from ccim import session
        with mock.patch.object(session.sys, "platform", "linux"):
            r = await c._pre_bash(data, "t1", None)
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("run_as_admin", r["hookSpecificOutput"]["permissionDecisionReason"])
        with mock.patch.object(session.sys, "platform", "darwin"):
            self.assertEqual(await c._pre_bash(data, "t1", None), {})


if __name__ == "__main__":
    unittest.main()
