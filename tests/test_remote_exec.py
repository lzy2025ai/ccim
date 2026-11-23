"""在用户的另一台电脑上执行命令、取回文件（#546）。"""
import asyncio
import base64
import time
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


class UploadJobTests(unittest.TestCase):
    """上传文件、后台任务（#552）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.object(registry, "HOME", self.tmp.name)
        self.home.start()
        remote.set_allow(True)

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def up(self, name, data):
        return {"name": name, "data": base64.b64encode(data).decode()}

    def test_upload_then_run(self):
        r = remote.execute({"command": "cat a.txt", "cwd": self.tmp.name, "upload": [self.up("a.txt", b"hello")]}, PEER)
        self.assertEqual(r["stdout"], "hello")
        self.assertEqual(r["uploaded"], [os.path.realpath(os.path.join(self.tmp.name, "a.txt"))])
        with self.assertRaises(remote.Problem):           # 同名不覆盖，整批不写
            remote.execute({"cwd": self.tmp.name, "upload": [self.up("b.txt", b"1"), self.up("a.txt", b"x")]}, PEER)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "b.txt")))
        r = remote.execute({"cwd": self.tmp.name, "upload": [self.up("a.txt", b"x")], "overwrite": True}, PEER)
        self.assertNotIn("code", r)                       # 只上传
        self.assertEqual(open(os.path.join(self.tmp.name, "a.txt"), "rb").read(), b"x")
        r = remote.execute({"cwd": self.tmp.name, "upload": [self.up("../evil.txt", b"x")]}, PEER)   # 名字只取最后一段
        self.assertEqual(os.path.dirname(r["uploaded"][0]), os.path.realpath(self.tmp.name))
        self.assertIn("upload", [x["kind"] for x in remote.recent()])

    def wait(self, job, stop=False):
        for _ in range(100):
            r = remote.job({"job": job, "stop": stop}, PEER)
            if not r["running"]:
                return r
            time.sleep(0.1)
        return r

    def test_background_job(self):
        r = remote.execute({"command": "echo start; sleep 0.3; echo done; exit 4", "cwd": self.tmp.name, "background": True}, PEER)
        job = r["job"]
        self.assertTrue(remote.job({"job": job}, PEER)["running"])
        r = self.wait(job)
        self.assertEqual((r["code"], r["output"]), (4, "start\ndone\n"))
        self.assertIn("跑完了，退出码 4", remote.report_job("Air", r))
        with self.assertRaises(remote.Problem):
            remote.job({"job": "../x"}, PEER)

    def test_stop_job(self):
        job = remote.execute({"command": "sleep 30", "background": True}, PEER)["job"]
        r = remote.job({"job": job, "stop": True}, PEER)
        self.assertTrue(r["stopped"])
        self.assertFalse(r["running"])
        self.assertIn("已停掉", remote.report_job("Air", r))

    def test_job_needs_switch(self):
        job = remote.execute({"command": "true", "background": True}, PEER)["job"]
        remote.set_allow(False)
        with self.assertRaises(remote.Problem):
            remote.job({"job": job}, PEER)


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
        self.assertEqual([t.name for t in tools], ["run_on_machine", "machine_job"])
        self.assertIn("「Air」", tools[0].description)
        self.job_tool = tools[1]
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

    def test_upload_and_job_round_trip(self):
        remote.set_allow(True)
        dest = os.path.join(self.tmp.name, "dest")
        os.makedirs(dest)
        src = os.path.join(self.tmp.name, "表格.xlsx")
        with open(src, "wb") as f:
            f.write(b"xlsx")

        def forward(mid, method, path, body, headers):
            fn = remote.execute if path == "/api/remote/exec" else remote.job
            return 200, "application/json", json.dumps(fn(json.loads(body), PEER)).encode()

        t = self._tool(forward)
        res = asyncio.run(t.handler({"machine": "Air", "upload": [src], "upload_to": dest}))
        self.assertIn(os.path.join(os.path.realpath(dest), "表格.xlsx"), res["content"][0]["text"])
        res = asyncio.run(t.handler({"machine": "Air", "command": "sleep 0.2; echo ok > r.txt", "cwd": dest, "background": True}))
        job = res["content"][0]["text"].split("任务编号 ")[1].split("，")[0]
        for _ in range(50):
            res = asyncio.run(self.job_tool.handler({"machine": "Air", "job": job, "files": ["r.txt"]}))
            if "跑完了" in res["content"][0]["text"]:
                break
            time.sleep(0.1)
        text = res["content"][0]["text"]
        self.assertIn("退出码 0", text)
        self.assertIn("r.txt →", text)
        res = asyncio.run(t.handler({"machine": "Air", "upload": ["/no/such/file"]}))
        self.assertTrue(res.get("is_error"))

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
