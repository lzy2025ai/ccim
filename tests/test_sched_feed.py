"""动态（#520）：定时任务结果排成信息流。不出声、出错的不算动态；每隔几分钟跑的一天合成一条；
配图、追问只取那次结果的；最近一次出错的任务单独列出来。"""
import time
import unittest

from ccim import board
from ccim.schedules import Schedules, feed_hidden, split_quote


def make(logs, data, chats):
    class L:
        def read(self, k):
            return logs.get(k, [])

    class Ch:
        pass

    class PF:
        channel = Ch()

        def chats(self):
            return chats

    PF.channel.logs = L()
    sc = Schedules.__new__(Schedules)
    sc.pf = PF()
    sc.all = lambda: data
    return sc


class FeedTests(unittest.TestCase):
    def test_feed(self):
        now = time.time()
        logs = {
            "schedule-a": [
                {"type": "run", "id": "a:0", "state": "done", "at": now - 90000, "done_at": now - 89990, "text": "昨天的", "chat": "s-a-x"},
                {"type": "run", "id": "a:1", "state": "done", "at": now - 100, "done_at": now - 90, "text": "结论\n- 详情", "chat": "s-a-x"}],
            "schedule-b": [
                {"type": "run", "id": "b:1", "state": "done", "at": now - 500, "done_at": now - 499, "text": "x1", "chat": "c1"},
                {"type": "run", "id": "b:2", "state": "done", "at": now - 300, "done_at": now - 299, "text": "x2", "chat": "c1"},
                {"type": "run", "id": "b:3", "state": "quiet", "at": now - 200, "done_at": now - 199, "text": ""},
                {"type": "run", "id": "b:4", "state": "error", "at": now - 50, "done_at": now - 49, "text": "boom"}],
            "s-a-x": [
                {"type": "user", "sched": "t", "at": now - 90000}, {"type": "answer", "text": "昨天的", "at": now - 89991},
                {"type": "user", "sched": "t", "at": now - 100},
                {"type": "file", "name": "p.png", "path": "/tmp/p.png", "at": now - 95},
                {"type": "answer", "text": "结论", "at": now - 91},
                {"type": "user", "text": "只有天猫？", "at": now - 50}, {"type": "answer", "text": "是", "at": now - 40}],
            "c1": [
                {"type": "answer", "text": "x2", "at": now - 300},
                {"type": "user", "text": "无关", "at": now - 280}, {"type": "answer", "text": "无关答", "at": now - 270},
                {"type": "user", "text": "追问", "feed_run": "b:2", "at": now - 260}, {"type": "answer", "text": "答", "at": now - 250}],
        }
        data = {"a": {"name": "竞品", "freq": "daily", "target": {"kind": "agent", "id": "x"}, "seen_at": now - 1000},
                "b": {"name": "巡检", "freq": "every", "target": {"kind": "chat", "id": "c1"}, "seen_at": now - 400}}
        sc = make(logs, data, {"s-a-x": {"agent": "x"}, "c1": {"agent": "y"}})
        sc._log = lambda sid: f"schedule-{sid}"
        r = sc.feed(["a", "b"])
        ps = {p["rid"]: p for p in r["posts"]}
        self.assertEqual([p["rid"] for p in r["posts"]], ["a:1", "b:2", "a:0"])   # 新的在前，b 一天合成一条
        self.assertEqual(ps["b:2"]["count"], 2)
        self.assertTrue(ps["a:1"]["new"])
        self.assertFalse(ps["a:0"]["new"])
        self.assertEqual(ps["a:1"]["images"], [{"path": "/tmp/p.png", "name": "p.png"}])
        self.assertEqual([x["text"] for x in ps["a:1"]["replies"]], ["只有天猫？", "是"])
        self.assertEqual(ps["a:0"]["images"], [])
        self.assertEqual(ps["a:0"]["replies"], [])     # 下一次运行之后的追问不算前一次的
        self.assertEqual([x["text"] for x in ps["b:2"]["replies"]], ["追问", "答"])   # 别的对话里只算在动态里追问的
        self.assertEqual(ps["b:2"]["agent"], "y")
        self.assertEqual([f["id"] for f in r["failing"]], ["b"])


    def test_hidden(self):
        """动态里回复的那几轮对话里不显示，带进对话的照常；等批准的卡片总显示"""
        rows = [{"type": "user", "text": "a"}, {"type": "answer", "text": "A"},
                {"type": "user", "text": "q1", "feed_run": "r1"}, {"type": "answer", "text": "x1"},
                {"type": "card", "buttons": [1]}, {"type": "note", "text": "n"},
                {"type": "user", "text": "q2", "feed_run": "r2"}, {"type": "answer", "text": "x2"},
                {"type": "user", "text": "b"}, {"type": "answer", "text": "B"}]
        self.assertEqual([r["text"] for r in feed_hidden(rows, []) if "text" in r], ["a", "A", "b", "B"])
        self.assertEqual([r["type"] for r in feed_hidden(rows, [])].count("card"), 1)
        self.assertEqual([r["text"] for r in feed_hidden(rows, ["r2"]) if "text" in r], ["a", "A", "q2", "x2", "b", "B"])

    def test_quote(self):
        self.assertEqual(split_quote("> 上一条\n> 第二行\n\n我的话"), ("> 上一条\n> 第二行", "我的话"))
        self.assertEqual(split_quote("没有引用"), ("", "没有引用"))

    def test_board(self):
        """动态里回复的那一轮回答不算要你看的，你在对话里说话后照常"""
        s = {"answer_at": 10, "feed_last": True}
        self.assertIsNone(board.attention({"seen_at": 0}, s))
        self.assertFalse(board.unread({"seen_at": 0}, s))
        self.assertEqual(board.attention({"seen_at": 0}, {"answer_at": 10, "pending": True, "feed_last": True}), "ask")
        self.assertEqual(board.attention({"seen_at": 0}, {"answer_at": 10}), "done")


if __name__ == "__main__":
    unittest.main()
