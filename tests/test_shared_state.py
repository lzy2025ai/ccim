"""同时到的整体状态请求只算一次（#258）：共用、几秒内复用、有改动就作废。"""
import threading
import time
import unittest
from unittest.mock import patch

from ccim import peers
from ccim.peers import Shared


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.s = Shared()
        self.calls = 0

    def slow(self, secs=0.3):
        def fn():
            self.calls += 1
            n = self.calls
            time.sleep(secs)
            return {"n": n}
        return fn

    def test_concurrent_share_one(self):
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.s.get("k", self.slow()))) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.calls, 1)
        self.assertEqual({o["n"] for o in out}, {1})

    def test_reuse_within_ttl_then_expire(self):
        self.s.get("k", self.slow(0))
        self.s.get("k", self.slow(0))
        self.assertEqual(self.calls, 1)
        with patch.object(peers.time, "time", return_value=time.time() + Shared.TTL + 1):
            self.s.get("k", self.slow(0))
        self.assertEqual(self.calls, 2)

    def test_keys_apart(self):
        self.s.get(("state", None), self.slow(0))
        self.s.get(("state", "guest"), self.slow(0))
        self.assertEqual(self.calls, 2)

    def test_change_invalidates(self):
        self.s.get("k", self.slow(0))
        self.s.changed()
        self.assertEqual(self.s.get("k", self.slow(0))["n"], 2)

    def test_started_before_change_not_reused(self):
        first = []
        t = threading.Thread(target=lambda: first.append(self.s.get("k", self.slow(0.4))))
        t.start()
        time.sleep(0.1)
        self.s.changed()                      # 算到一半有了改动：之后来的不能用这份
        later = self.s.get("k", self.slow(0))
        t.join()
        self.assertEqual(first[0]["n"], 1)
        self.assertEqual(later["n"], 2)

    def test_working_progress_does_not_invalidate(self):
        self.s.get("k", self.slow(0))
        self.s.changed({"type": "progress", "state": "working", "chat": "c"})
        self.s.get("k", self.slow(0))
        self.assertEqual(self.calls, 1)
        self.s.changed({"type": "progress", "state": "done", "chat": "c"})
        self.s.get("k", self.slow(0))
        self.assertEqual(self.calls, 2)

    def test_error_not_cached(self):
        def boom():
            self.calls += 1
            raise ValueError("x")
        with self.assertRaises(ValueError):
            self.s.get("k", boom)
        self.assertEqual(self.s.get("k", self.slow(0))["n"], 2)

    def test_events_hook(self):
        from ccim.platform import Events
        ev = Events()
        ev.on_publish.append(self.s.changed)
        self.s.get("k", self.slow(0))
        ev.publish({"type": "answer", "chat": "c"})
        self.s.get("k", self.slow(0))
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
