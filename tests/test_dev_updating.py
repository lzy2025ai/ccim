"""开发页（#156 第三轮）：后台升级还没做完时，不说「有改动没用上」，做完很快重算。"""
import fcntl
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from ccim import devpanel, runtime


class UpdatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(runtime, "_stable", lambda: self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_promoting_follows_lock(self):
        self.assertFalse(runtime.promoting())
        with open(os.path.join(self.tmp.name, ".promote.lock"), "w") as f:   # promote-run 拿着锁
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(runtime.promoting())
        self.assertFalse(runtime.promoting())

    def test_badge_and_refresh_while_updating(self):
        d = devpanel.Dev.__new__(devpanel.Dev)
        d.cache, d.at, d.job = {"review": [], "merged": [{"commit": "x"}], "updating": True}, time.time() - 10, None
        self.assertEqual(d.badge(), 0)                    # 正在更新：不算「没用上」
        calls = []
        d.refresh = lambda: calls.append(1)
        d.overview()
        self.assertEqual(calls, [1])                      # 更新中 5 秒就重算
        d.cache["updating"], d.at = False, time.time() - 10
        d.overview()
        self.assertEqual(calls, [1])                      # 平时照旧缓存
        self.assertEqual(d.badge(), 1)


if __name__ == "__main__":
    unittest.main()
