"""日记集中存在一台电脑上（#376）：按时间插入、暂存补写、设置在总控和加入的电脑之间同步、转给存日记的那台写、
Obsidian 库列表只在安全时改。不碰真实主目录。"""
import datetime
import json
import os
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from ccim import diary, peers, registry
from ccim.platform import Events, Platform, Problem


def fake_pf():
    return types.SimpleNamespace(channel=types.SimpleNamespace(events=Events()))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.dict(os.environ, {"HOME": self.tmp.name}),
                  patch.object(registry, "HOME", os.path.join(self.tmp.name, ".ccim")),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, ".ccim", "registry.json"))):
            p.start()
            self.addCleanup(p.stop)

    def day(self, d):
        with open(os.path.join(diary.root(), d + ".md"), encoding="utf-8") as fh:
            return fh.read()


class FileTests(Base):
    def test_insert_by_time(self):
        d = datetime.datetime(2026, 10, 31, 14, 0)
        diary.add("下午", "", d)
        diary.add("晚上", "", d.replace(hour=20))
        diary.add("早上（补写的）", "飞书 · p", d.replace(hour=9, minute=5))
        diary.add("中午（补写的）", "", d.replace(hour=12))
        self.assertEqual([e["time"] for e in diary._entries("2026-10-31")], ["09:05", "12:00", "14:00", "20:00"])
        self.assertEqual(self.day("2026-10-31"),
                         "## 09:05 · 飞书 · p\n\n早上（补写的）\n\n## 12:00\n\n中午（补写的）\n\n## 14:00\n\n下午\n\n## 20:00\n\n晚上\n")

    def test_append_after_hand_edit_without_newline(self):
        os.makedirs(diary.root())
        with open(os.path.join(diary.root(), "2026-10-31.md"), "w", encoding="utf-8") as fh:
            fh.write("## 8:00\n早饭")
        diary.add("午饭", "", datetime.datetime(2026, 10, 31, 12, 0))
        self.assertEqual([e["text"] for e in diary._entries("2026-10-31")], ["早饭", "午饭"])

    def test_pending(self):
        self.assertEqual(diary.take_pending(), [])
        diary.hold({"text": "a", "at": "2026-10-31T09:00:00"})
        diary.hold({"text": "b"})
        self.assertEqual([x["text"] for x in diary.take_pending()], ["a", "b"])
        self.assertEqual(diary.take_pending(), [])

    def test_when(self):
        self.assertEqual(diary.when("2026-10-31T09:05:00"), datetime.datetime(2026, 10, 31, 9, 5))
        self.assertIsInstance(diary.when("乱写"), datetime.datetime)

    def test_record_from_channel(self):
        # 存日记的是这台：直接写
        r = diary.record("飞书里记的", "飞书 · p")
        self.assertNotIn("pending", r)
        self.assertIn("飞书里记的", diary.read())
        # 在别的电脑：交给本机平台；平台没在运行就先存着
        with patch.object(diary, "_here", return_value=False), \
                patch.object(diary, "_platform_api", side_effect=OSError("refused")):
            r = diary.record("平台没开", "飞书 · p")
            self.assertTrue(r["pending"])
            self.assertEqual([x["text"] for x in diary.take_pending()], ["平台没开"])
            with self.assertRaises(ValueError):
                diary.query("read")
        with patch.object(diary, "_here", return_value=False), \
                patch.object(diary, "_platform_api", return_value={"text": "那台的日记"}) as api:
            self.assertEqual(diary.query("search", query="词"), "那台的日记")
            self.assertEqual(api.call_args[0], ("/api/diary/query", {"query": "词", "op": "search"}))


class HomeFileTests(Base):
    def test_home_file(self):
        me = peers.identity()["id"]
        self.assertEqual(peers.diary_home_file(), me)                     # 只有一台
        peers._save("peers.json", {"m2": {"name": "B", "key": "k"}})
        peers._save("diary.json", {"owner": "m2"})
        self.assertEqual(peers.diary_home_file(), "m2")                   # 总控设成了加入的那台
        peers._save("diary.json", {"owner": "gone"})
        self.assertEqual(peers.diary_home_file(), me)                     # 设的那台被移除了
        peers._save("hub.json", {"hub_id": "h1", "url": "http://x", "key": "k"})
        self.assertEqual(peers.diary_home_file(), "h1")                   # 加入了局域网里的总控，它还没说：默认它
        peers._save("diary-home.json", {"hub": "h1", "machine": me})
        self.assertEqual(peers.diary_home_file(), me)
        peers._save("diary-home.json", {"hub": "old", "machine": me})
        self.assertEqual(peers.diary_home_file(), "h1")                   # 以前加入的那个总控说的不算
        peers._save("hub.json", {"hub_id": "h1", "url": "https://x", "key": "k", "server": True})
        self.assertIsNone(peers.diary_home_file())                        # 服务器上的总控还没说：还不知道


class PeerTests(Base):
    def pair(self, server=False):
        hub = peers.Peers(fake_pf(), server=server)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), peers._handler(hub))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        node = peers.Peers(fake_pf())
        node.me = {"id": "node-1", "name": "那台电脑"}
        key = "k" * 64
        hub.nodes = {"node-1": {"name": "那台电脑", "key": key}}
        node.hub = {"url": f"http://127.0.0.1:{srv.server_address[1]}", "key": key, "hub_id": hub.me["id"], "hub_name": "总控"}
        node.hub_ok = time.time()             # 第一次长轮询要挂满才回来，这里直接算连着
        self.addCleanup(setattr, node, "stopping", True)
        threading.Thread(target=node._poll_loop, daemon=True).start()
        for _ in range(50):
            if hub.online("node-1") and node.hub_ok:
                break
            time.sleep(0.1)
        return hub, node

    def wait(self, fn):
        for _ in range(100):
            if fn():
                return True
            time.sleep(0.05)
        return False

    def test_lan_hub_default_and_change(self):
        hub, node = self.pair()
        self.assertEqual(hub.diary_home(), hub.me["id"])                  # 默认总控那台
        self.assertEqual(node.diary_home(), hub.me["id"])                 # 加入的电脑还没听总控说：局域网里默认总控
        hub.set_diary_home(hub.me["id"])
        self.assertTrue(self.wait(lambda: peers._load("diary-home.json", {}).get("machine") == hub.me["id"]))   # 长轮询里告诉加入的电脑
        self.assertEqual(node.set_diary_home("node-1"), "node-1")         # 在加入的电脑上改：交给总控存
        self.assertEqual(hub.diary_home(), "node-1")
        self.assertEqual(node.diary_home(), "node-1")
        with self.assertRaises(ValueError):
            node.set_diary_home("nope")
        hub.remove("node-1")
        self.assertEqual(hub.diary_home(), hub.me["id"])                  # 那台被移除了：回到总控

    def test_server_hub_first_machine(self):
        hub, node = self.pair(server=True)
        hub.nodes["node-2"] = {"name": "第二台", "key": "x" * 64}
        self.assertEqual(hub.diary_home(), "node-1")                      # 服务器上：主人最早加入的那台
        node.hub["server"] = True
        self.assertIsNone(node.diary_home())                              # 还没听总控说
        hub.set_diary_home("node-1")
        self.assertTrue(self.wait(lambda: node.diary_home() == "node-1"))
        self.assertEqual(node.set_diary_home("node-2"), "node-2")
        self.assertEqual(hub.diary_home(), "node-2")
        with self.assertRaises(ValueError):
            hub.set_diary_home(hub.me["id"])                              # 服务器自己不是一台电脑

    def test_write_goes_to_home_through_hub(self):
        """加入的电脑上记的日记经总控转到存日记的那台（这里是总控自己）写，带原来的时间，本机网址换成那台的。"""
        hub, node = self.pair()
        self.assertEqual(node.diary_home(), hub.me["id"])
        got = []

        def at_home(pf, method, path, body=b"", headers=None, timeout=None):
            b = json.loads(body)
            got.append((path, b))
            r = Platform.diary_write(fake_platform(hub, "http://127.0.0.1:7000"), b["text"], b["where"], b["at"], b["link"], b["here"])
            return 200, "application/json", json.dumps(r).encode()
        node_pf = fake_platform(node, "http://127.0.0.1:8000")
        with patch.object(peers, "local_request", side_effect=at_home):
            r = Platform.diary_write(node_pf, "在另一台上记的", "小助手「周计划」", "2026-10-31T09:05:00",
                                     "http://127.0.0.1:8000/#/agents/a1/c1?m=d-1")
        self.assertEqual(r, {"date": "2026-10-31", "time": "09:05"})
        self.assertEqual(got[0][0], "/api/diary/add")
        self.assertIn("## 09:05 · [小助手「周计划」](http://127.0.0.1:7000/#/agents/a1/c1?m=d-1)", self.day("2026-10-31"))

    def test_offline_then_flush(self):
        """存日记的那台不在线：先存着，连上后按原来的时间补写。"""
        pf = fake_platform(types.SimpleNamespace(diary_home=lambda: "m2", me={"id": "m1"}, online=lambda m: False,
                                                 forward=lambda *a, **k: (_ for _ in ()).throw(peers.Offline())), "http://127.0.0.1:1")
        r = Platform.diary_write(pf, "离线时记的", "", "2026-10-30T22:10:00")
        self.assertTrue(r["pending"])
        self.assertEqual(r["machine"], "B")
        with self.assertRaises(Problem):
            Platform.diary_query(pf, "read", {})
        items = diary.take_pending()
        self.assertEqual(items[0]["at"], "2026-10-30T22:10:00")
        # 连上了：设成写在本机，再补写
        pf.peers.diary_home = lambda: "m1"
        for it in items:
            Platform.diary_write(pf, it["text"], it["where"], it["at"], it.get("link"))
        self.assertIn("## 22:10\n\n离线时记的", self.day("2026-10-30"))
        self.assertIn("离线时记的", Platform.diary_query(pf, "read", {"start": "2026-10-30"})["text"])


def fake_platform(p, root):
    return types.SimpleNamespace(peers=p, _machine_name=lambda mid: "B", _web_root=lambda: root)


class ObsidianTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.tmp.name, "obsidian")
        os.makedirs(self.dir)
        self.cfg = os.path.join(self.dir, "obsidian.json")
        self.folder = os.path.join(self.tmp.name, "Documents", "ccim-日记")
        os.makedirs(self.folder)
        self.orig = {"vaults": {"aaaa": {"path": "/somewhere/else", "ts": 1, "open": True}}, "cli": True}
        with open(self.cfg, "w") as fh:
            json.dump(self.orig, fh)

    def load(self):
        with open(self.cfg) as fh:
            return json.load(fh)

    def test_running_untouched(self):
        self.assertIsNone(diary.obsidian_vault(self.folder, self.cfg, running=True))
        self.assertEqual(self.load(), self.orig)
        self.assertFalse(os.path.exists(self.cfg + ".ccim-bak"))

    def test_add_when_closed(self):
        vid = diary.obsidian_vault(self.folder, self.cfg, running=False)
        d = self.load()
        self.assertEqual(d["cli"], True)
        self.assertEqual(d["vaults"]["aaaa"], self.orig["vaults"]["aaaa"])
        self.assertEqual(d["vaults"][vid]["path"], self.folder)
        with open(self.cfg + ".ccim-bak") as fh:
            self.assertEqual(json.load(fh), self.orig)
        # 已经是库了：直接用，不再改
        self.assertEqual(diary.obsidian_vault(self.folder, self.cfg, running=True), vid)

    def test_bad_config_untouched(self):
        with open(self.cfg, "w") as fh:
            fh.write("{坏的")
        self.assertIsNone(diary.obsidian_vault(self.folder, self.cfg, running=False))
        with open(self.cfg) as fh:
            self.assertEqual(fh.read(), "{坏的")

    def test_never_opened(self):
        cfg = os.path.join(self.tmp.name, "nope", "obsidian.json")
        self.assertIsNone(diary.obsidian_vault(self.folder, cfg, running=False))
        self.assertFalse(os.path.exists(cfg))

    def test_apps(self):
        with patch.object(diary, "_system", return_value="Linux"), patch("shutil.which", side_effect=lambda n: "/usr/bin/code" if n == "code" else None):
            self.assertEqual([a["id"] for a in diary.apps()], ["files", "vscode"])
        with patch.object(diary, "_system", return_value="Darwin"), patch.object(diary, "_mac_app", side_effect=lambda n, b: n == "Obsidian"):
            self.assertEqual(diary.apps(), [{"id": "files", "name": "访达"}, {"id": "obsidian", "name": "Obsidian"}])
        with patch.object(diary, "apps", return_value=[{"id": "files", "name": "访达"}]):
            with self.assertRaises(ValueError):
                diary.open_in("vscode")


if __name__ == "__main__":
    unittest.main()
