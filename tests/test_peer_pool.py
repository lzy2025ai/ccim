"""电脑和总控之间复用连接（#264）：用完放回去接着用、放久了丢掉、复用的断了只对没副作用的重发；不碰真实主目录。"""
import http.client
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


def fake_pf():
    return types.SimpleNamespace(channel=types.SimpleNamespace(events=Events()))


class PeerPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(registry, "HOME", self.tmp.name),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, "registry.json"))):
            p.start()
            self.addCleanup(p.stop)
        self.hub = peers.Peers(fake_pf(), server=True)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), peers._handler(self.hub))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.node = peers.Peers(fake_pf())
        self.node.me = {"id": "node-1", "name": "那台电脑"}
        self.addCleanup(lambda: [c.close() for c, _ in self.node.pool])
        key = "k" * 64
        self.hub.nodes = {"node-1": {"name": "那台电脑", "key": key}}
        self.node.hub = {"url": f"http://127.0.0.1:{self.srv.server_address[1]}", "key": key, "hub_id": self.hub.me["id"],
                         "hub_name": "总控"}
        self.made = 0
        real = peers._conn

        def counted(url, timeout):
            self.made += 1
            return real(url, timeout)
        p = patch.object(peers, "_conn", counted)
        p.start()
        self.addCleanup(p.stop)

    def test_reused(self):
        for _ in range(5):
            status, _, _ = self.node._hub_request("/peer/push", b"[]", {}, 10)
            self.assertEqual(status, 200)
        self.assertEqual(self.made, 1)            # 5 次只建了 1 条连接

    def test_idle_dropped(self):
        self.node._hub_request("/peer/push", b"[]", {}, 10)
        with patch.object(peers.time, "time", return_value=time.time() + peers.POOL_IDLE + 1):
            self.node._hub_request("/peer/push", b"[]", {}, 10)
        self.assertEqual(self.made, 2)

    def broken_pool(self):
        """池子里放一条已经被对面断开的连接。"""
        self.node._hub_request("/peer/push", b"[]", {}, 10)
        c, _ = self.node.pool[0]
        c.sock.close()
        c.sock = None
        c.auto_open = 0                           # 模拟对面已经断开：直接发会报错，不会自己重连

    def test_safe_path_retried_with_new_nonce(self):
        self.broken_pool()
        status, _, _ = self.node._hub_request("/peer/push", b"[]", {}, 10)   # 新随机数：不会被总控当成重放
        self.assertEqual(status, 200)
        self.assertEqual(self.made, 2)

    def test_call_not_retried(self):
        self.broken_pool()
        with self.assertRaises((ConnectionError, http.client.HTTPException)):
            self.node._hub_request("/peer/call", b"", {"X-Target": "x", "X-Method": "POST", "X-Path": "/api/chat/send"}, 10)
        self.assertEqual(self.made, 1)            # 转交的操作可能已经执行了：不重发

    def test_concurrent_requests_dont_share(self):
        errs = []

        def go():
            try:
                for _ in range(5):
                    self.node._hub_request("/peer/push", b"[]", {}, 10)
            except Exception as e:
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertLessEqual(len(self.node.pool), peers.POOL_MAX)


if __name__ == "__main__":
    unittest.main()
