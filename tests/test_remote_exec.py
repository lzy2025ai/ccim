"""在用户的另一台电脑上执行命令、取回文件（#546）。"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ccim import registry, remote

PEER = {"X-From-Peer": "1"}


class ExecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.object(registry, "HOME", self.tmp.name)
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_off_by_default_and_only_from_owner_peers(self):
        self.assertFalse(remote.allowed())
        with self.assertRaises(remote.Problem):
            remote.execute({"command": "echo hi"}, PEER)            # 没打开
        remote.set_allow(True)
        with self.assertRaises(remote.Problem):
            remote.execute({"command": "echo hi"}, {})               # 不是别的电脑转来的
        with self.assertRaises(remote.Problem):
            remote.execute({"command": "echo hi"}, dict(PEER, **{"X-Account": "a1"}))   # 访客
        remote.set_allow(False)
        with self.assertRaises(remote.Problem):
            remote.execute({"command": "echo hi"}, PEER)

    def test_runs_and_records(self):
        remote.set_allow(True)
        r = remote.execute({"command": "echo out; echo err >&2; exit 3", "cwd": self.tmp.name, "from": "Pro", "by": "花花"}, PEER)
        self.assertEqual((r["code"], r["stdout"], r["stderr"], r["killed"]), (3, "out\n", "err\n", False))
        log = [x for x in remote.info()["log"] if x["kind"] == "exec"]
        self.assertEqual((log[0]["from"], log[0]["by"], log[0]["code"]), ("Pro", "花花", 3))
        self.assertEqual(oct(os.stat(os.path.join(self.tmp.name, "remote-log.jsonl")).st_mode & 0o777), "0o600")

    def test_timeout_kills(self):
        remote.set_allow(True)
        r = remote.execute({"command": "sleep 30", "timeout": 1}, PEER)
        self.assertTrue(r["killed"])
        self.assertLess(r["took"], 10)

    def test_files_come_back(self):
        remote.set_allow(True)
        with open(os.path.join(self.tmp.name, "a.png"), "wb") as f:
            f.write(b"\x89PNG data")
        with mock.patch.object(remote, "FILE_LIMIT", 100):
            with open(os.path.join(self.tmp.name, "big.bin"), "wb") as f:
                f.write(b"x" * 200)
            r = remote.execute({"command": "true", "cwd": self.tmp.name, "files": ["a.png", "big.bin", "nope.txt"]}, PEER)
        text = remote.report("Air", r)
        got = remote.save_files(r["files"])
        self.assertTrue(got[0][1] and open(got[0][1], "rb").read() == b"\x89PNG data")
        self.assertIn("太大", got[1][2])
        self.assertIn("不存在", got[2][2])
        self.assertIn("a.png →", text)

    def test_find_machine(self):
        peers = SimpleNamespace(machines=lambda own=False: [
            {"id": "m1", "name": "Jone的MacBook Pro", "me": True},
            {"id": "m2", "name": "蔡婷的MacBook Air", "me": False},
            {"id": "m3", "name": "别人的电脑", "me": False, "shared": True}])
        self.assertEqual(remote.find(peers, "Air")[0]["id"], "m2")
        self.assertEqual(remote.find(peers, "蔡婷的MacBook Air")[0]["id"], "m2")
        self.assertIsNone(remote.find(peers, "别人的电脑")[0])        # 别人共享过来的不算
        self.assertIn("直接用 Bash", remote.find(peers, "Jone的MacBook Pro")[1])
        self.assertIn("蔡婷的MacBook Air", remote.find(peers, "Windows")[1])


class ToolTests(unittest.TestCase):
    """Agent 这边的工具：经 peers.forward 交给那台电脑，结果和文件拿回本机。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.object(registry, "HOME", self.tmp.name)
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def _tool(self, forward):
        from ccim import session
        peers = SimpleNamespace(me={"id": "m1", "name": "Pro"}, forward=forward,
                                machines=lambda own=False: [{"id": "m1", "name": "Pro", "me": True},
                                                            {"id": "m2", "name": "Air", "me": False}])
        chat = SimpleNamespace(b=SimpleNamespace(entry={}, path=self.tmp.name))
        tools = session._remote_tools(chat, SimpleNamespace(peers=peers))
        self.assertEqual([t.name for t in tools], ["run_on_machine"])
        self.assertIn("「Air」", tools[0].description)
        return tools[0]

    def test_round_trip(self):
        remote.set_allow(True)
        src = os.path.join(self.tmp.name, "shot.png")
        with open(src, "wb") as f:
            f.write(b"img")
        seen = {}

        def forward(mid, method, path, body, headers):
            seen.update(mid=mid, path=path)
            r = remote.execute(json.loads(body), PEER)      # 假装是那台电脑
            return 200, "application/json", json.dumps(r).encode()

        t = self._tool(forward)
        res = asyncio.run(t.handler({"machine": "Air", "command": "echo 你好", "files": [src]}))
        text = res["content"][0]["text"]
        self.assertEqual((seen["mid"], seen["path"]), ("m2", "/api/remote/exec"))
        self.assertIn("你好", text)
        local = text.split(" → ")[1].strip()
        self.assertTrue(local.startswith(os.path.join(self.tmp.name, "tmp", "remote")))
        self.assertEqual(open(local, "rb").read(), b"img")
        self.assertNotIn("is_error", res)

    def test_errors(self):
        t = self._tool(lambda *a: (400, "application/json", json.dumps({"error": "没有打开"}).encode()))
        res = asyncio.run(t.handler({"machine": "Air", "command": "ls"}))
        self.assertTrue(res.get("is_error"))
        self.assertIn("没有打开", res["content"][0]["text"])
        t = self._tool(lambda *a: (404, "text/plain", b"not found"))
        self.assertIn("版本较旧", asyncio.run(t.handler({"machine": "Air", "command": "ls"}))["content"][0]["text"])
        res = asyncio.run(t.handler({"machine": "Pro", "command": "ls"}))
        self.assertIn("直接用 Bash", res["content"][0]["text"])

    def test_single_machine_gets_no_tool(self):
        from ccim import session
        peers = SimpleNamespace(me={"id": "m1"}, machines=lambda own=False: [{"id": "m1", "name": "Pro", "me": True}])
        chat = SimpleNamespace(b=SimpleNamespace(entry={}, path=self.tmp.name))
        self.assertEqual(session._remote_tools(chat, SimpleNamespace(peers=peers)), [])


if __name__ == "__main__":
    unittest.main()
