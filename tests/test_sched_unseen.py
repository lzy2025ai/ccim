"""定时任务跑出了没看的结果（#447）：完成、出错算，不出声的不算；看过定时任务或它发结果的对话就不算。"""
import unittest

from ccim.schedules import Schedules


class UnseenTests(unittest.TestCase):
    def test_rules(self):
        u = lambda *a: Schedules._unseen(*a)[0]
        done = [{"type": "run", "state": "done", "at": 100, "done_at": 120, "chat": "w-1"}]
        self.assertEqual(u({"seen_at": 50}, done, {}), 1)
        self.assertEqual(u({"seen_at": 130}, done, {}), 0)
        self.assertEqual(u({"seen_at": 50}, done, {"w-1": {"seen_at": 125}}), 0)
        self.assertEqual(u({"seen_at": 50}, [{"state": "quiet", "at": 200}], {}), 0)
        self.assertEqual(u({"seen_at": 50}, [{"state": "error", "at": 200}], {}), 1)
        self.assertEqual(u({"seen_at": 50}, [], {}), 0)
        # 正在跑的这次不算
        self.assertEqual(u({"seen_at": 50}, done + [{"state": "working", "at": 300}], {}), 1)

    def test_count(self):
        """几次没看就是几；之后不出声的那次不把前面的清掉；看过之后的才算"""
        runs = [{"state": "done", "at": 100}, {"state": "error", "at": 200}, {"state": "done", "at": 300}, {"state": "quiet", "at": 400}]
        self.assertEqual(Schedules._unseen({"seen_at": 50}, runs, {}), (3, 300))
        self.assertEqual(Schedules._unseen({"seen_at": 150}, runs, {}), (2, 300))
        self.assertEqual(Schedules._unseen({"seen_at": 500}, runs, {}), (0, 0))


if __name__ == "__main__":
    unittest.main()
