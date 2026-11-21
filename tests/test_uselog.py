"""功能使用（#533）：只认合法的功能编号，按 7 天、30 天统计，记下最后一次。"""
import os, tempfile, time, unittest

from ccim import registry, uselog as usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.old, registry.HOME = registry.HOME, tempfile.mkdtemp()

    def tearDown(self):
        registry.HOME = self.old

    def test_record_summary(self):
        now = time.time()
        usage.record([{"f": "board.feed"}, {"f": "board.feed", "at": now - 3600}, {"f": "act.send"},
                      {"f": "Bad Key"}, {"f": 3}, "x", {"f": "feed.reply", "at": now - 5 * 86400 * 30}])
        d = usage.summary()["features"]
        self.assertEqual(d["board.feed"]["d7"], 2)
        self.assertEqual(d["act.send"]["d30"], 1)
        self.assertNotIn("Bad Key", d)
        self.assertEqual(d["feed.reply"]["d7"], 1)        # 太早的时间不信，按现在记
        self.assertTrue(os.listdir(os.path.join(registry.HOME, "usage")))

    def test_empty(self):
        self.assertEqual(usage.summary(), {"features": {}, "since": None})


if __name__ == "__main__":
    unittest.main()
