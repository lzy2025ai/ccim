"""经总控取整体状态再快一点（#260）：电脑回传先压缩（总控说支持才压），放久了的先用缓存、后台再取；不碰真实主目录。"""
import gzip
import json
import os
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from ccim import peers, registry
from ccim.platform import Events

BIG = json.dumps({"chats": [{"id": i, "title": "对话标题" * 5, "brief": "摘要" * 30} for i in range(500)]},
                 ensure_ascii=False).encode()


def fake_pf():
    return types.SimpleNamespace(channel=types.SimpleNamespace(events=Events()))


class PeerFasterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(registry, "HOME", self.tmp.name),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, "registry.json"))):
            p.start()
            self.addCleanup(p.stop)

    def pair(self):
        """一个总控（在本机随便一个端口上收 /peer/）和一台连着它的电脑，用同一把密钥。"""
        hub = peers.Peers(fake_pf(), server=True)
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
        self.addCleanup(setattr, node, "stopping", True)
        threading.Thread(target=node._poll_loop, daemon=True).start()
        for _ in range(50):
            if hub.online("node-1"):
                break
            time.sleep(0.1)
        return hub, node

    def test_reply_compressed_end_to_end(self):
        hub, node = self.pair()
        sent = []
        orig = node._hub_request
        node._hub_request = lambda path, body=b"", headers=None, timeout=30: (
            sent.append((path, len(body), (headers or {}).get("X-Enc"))), orig(path, body, headers, timeout))[1]
        with patch.object(peers, "local_request", return_value=(200, "application/json", BIG)):
            status, ctype, data = hub.forward("node-1", "GET", "/api/state?local=1")
        self.assertEqual((status, data), (200, BIG))
        reply = [s for s in sent if s[0] == "/peer/reply"][-1]
        self.assertEqual(reply[2], "gzip")
        self.assertLess(reply[1], len(BIG) / 3)

    def test_no_compress_for_old_hub_or_small(self):
        node = peers.Peers(fake_pf())
        node.hub = {"url": "http://127.0.0.1:1", "key": "k", "hub_id": "h", "hub_name": "总控"}
        got = []
        node._hub_request = lambda path, body=b"", headers=None, timeout=30: got.append((body, dict(headers or {})))
        m = {"rid": "r", "method": "GET", "path": "/api/state?local=1", "body": ""}
        for hub_gzip, payload in ((False, BIG), (True, b'{"ok": 1}'), (True, BIG)):
            node.hub_gzip = hub_gzip
            with patch.object(peers, "local_request", return_value=(200, "application/json", payload)):
                node._serve_req(m)
        (b1, h1), (b2, h2), (b3, h3) = got
        self.assertEqual((b1, h1.get("X-Enc")), (BIG, None))           # 总控没说支持（还没升级）：原样传
        self.assertEqual((b2, h2.get("X-Enc")), (b'{"ok": 1}', None))   # 小的不压
        self.assertEqual((gzip.decompress(b3), h3.get("X-Enc")), (BIG, "gzip"))

    def merged(self, cache_age, dirty, delay=0.5, fail=False, p=None):
        p = p or peers.Peers(fake_pf())
        p.me = {"id": "me", "name": "这台"}
        p.machines = lambda account=None: [{"id": "me", "me": True, "online": True}, {"id": "m2", "me": False, "online": True}]
        p.cache = {"m2": {"chats": [{"id": "old"}]}}
        p.cache_at = {"m2": time.time() - cache_age}
        if dirty:
            p.dirty.add("m2")
        calls = []

        def forward(mid, method, path, body=b"", headers=None):
            calls.append(threading.current_thread() is threading.main_thread())
            time.sleep(delay)
            if fail:
                raise peers.Offline()
            return 200, "application/json", json.dumps({"chats": [{"id": "new"}]}).encode()
        p.forward = forward
        t = time.time()
        out = p.merged_state({"agents": [], "chats": [], "projects": [], "schedules": []})
        return time.time() - t, [c["id"] for c in out["chats"]], calls

    def test_stale_cache_served_then_refreshed(self):
        secs, ids, calls = self.merged(cache_age=120, dirty=False)
        self.assertLess(secs, 0.3)                 # 不等那台
        self.assertEqual(ids, ["old"])
        time.sleep(0.7)
        self.assertEqual(calls, [False])           # 后台取了一次

    def test_dirty_still_waits(self):
        secs, ids, calls = self.merged(cache_age=1, dirty=True)
        self.assertGreaterEqual(secs, 0.5)         # 刚改过：现取
        self.assertEqual(ids, ["new"])

    def test_dirty_slow_uses_cache_then_pushes(self):
        """刚改过但那台慢（#335）：最多等 DIRTY_WAIT 就先用上次的，晚到的写进缓存并推一下让网页重取。"""
        p = peers.Peers(fake_pf())
        sent = []
        p.pf.channel.events.publish = sent.append
        secs, ids, _ = self.merged(cache_age=1, dirty=True, delay=peers.DIRTY_WAIT + 0.5, p=p)
        self.assertLess(secs, peers.DIRTY_WAIT + 0.3)
        self.assertEqual(ids, ["old"])
        time.sleep(0.8)
        self.assertEqual(p.cache["m2"], {"chats": [{"id": "new"}]})
        self.assertEqual(sent, [{"type": "machines"}])
        self.assertNotIn("m2", p.dirty)

    def test_dirty_slow_and_failed_asks_again(self):
        p = peers.Peers(fake_pf())
        secs, ids, _ = self.merged(cache_age=1, dirty=True, delay=peers.DIRTY_WAIT + 0.3, fail=True, p=p)
        self.assertEqual(ids, ["old"])
        time.sleep(0.6)
        self.assertIn("m2", p.dirty)               # 没取到：下次还现问

    def test_never_fetched_waits(self):
        p = peers.Peers(fake_pf())
        p.cache = {}
        p.me = {"id": "me", "name": "这台"}
        p.machines = lambda account=None: [{"id": "me", "me": True, "online": True}, {"id": "m2", "me": False, "online": True}]
        p.forward = lambda *a, **k: (time.sleep(peers.DIRTY_WAIT + 0.3), (200, "application/json", b'{"chats": [{"id": "new"}]}'))[1]
        out = p.merged_state({"agents": [], "chats": [], "projects": [], "schedules": []})
        self.assertEqual([c["id"] for c in out["chats"]], ["new"])


if __name__ == "__main__":
    unittest.main()
