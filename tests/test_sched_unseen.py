"""定时任务跑出了没看的结果（#447）：完成、出错算，不出声的不算；看过定时任务或它发结果的对话就不算。"""
import unittest

from ccim.schedules import Schedules


class UnseenTests(unittest.TestCase):
    def test_rules(self):
        u = Schedules._unseen
        done = [{"type": "run", "state": "done", "at": 100, "done_at": 120, "chat": "w-1"}]
        self.assertTrue(u({"seen_at": 50}, done, {}))
        self.assertFalse(u({"seen_at": 130}, done, {}))
        self.assertFalse(u({"seen_at": 50}, done, {"w-1": {"seen_at": 125}}))
        self.assertFalse(u({"seen_at": 50}, [{"state": "quiet", "at": 200}], {}))
        self.assertTrue(u({"seen_at": 50}, [{"state": "error", "at": 200}], {}))
        self.assertFalse(u({"seen_at": 50}, [], {}))
        # 正在跑的这次不算，看上一次跑完的
        self.assertTrue(u({"seen_at": 50}, done + [{"state": "working", "at": 300}], {}))


if __name__ == "__main__":
    unittest.main()
