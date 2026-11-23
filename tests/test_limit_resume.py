"""额度用完停下的对话（#563）：认出报错、算刷新时间、错开唤醒、派来的活等接着做完再交结果、总台右栏分组。不跑真实对话。"""
import asyncio
import datetime
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from ccim import board, desk, limits
from ccim.platform import Logs, Platform
from ccim.session import Chat, Task

SH = ZoneInfo("Asia/Shanghai")


def sh(*a):
    return datetime.datetime(*a, tzinfo=SH).timestamp()


class ParseTests(unittest.TestCase):
    def test_hit(self):
        self.assertTrue(limits.hit("You've hit your 5-hour limit · resets 1:20am (Asia/Shanghai)"))
        self.assertTrue(limits.hit("Prompt is too long · automatic compaction failed: You've hit your 5-hour limit · resets 1:50am (Asia/Shanghai)"))
        self.assertTrue(limits.hit("You've hit your weekly limit · resets Nov 27, 3pm (Asia/Shanghai)"))
        self.assertFalse(limits.hit("Prompt is too long"))
        self.assertFalse(limits.hit(None))

    def test_until_from_text(self):
        now = sh(2026, 11, 24, 1, 15)
        self.assertEqual(limits.until("You've hit your 5-hour limit · resets 1:20am (Asia/Shanghai)", now), sh(2026, 11, 24, 1, 20))
        self.assertEqual(limits.until("You've hit your 5-hour limit · resets 12am (Asia/Shanghai)", sh(2026, 11, 24, 22, 0)),
                         sh(2026, 11, 25, 0, 0))             # 只写了几点、已经过了：明天
        self.assertEqual(limits.until("You've hit your weekly limit · resets Nov 27, 3pm (Asia/Shanghai)", now),
                         sh(2026, 11, 27, 15, 0))
        self.assertEqual(limits.until("You've hit your weekly limit · resets Jan 2, 9:30am (Asia/Shanghai)", sh(2026, 12, 30, 8, 0)),
                         sh(2027, 1, 2, 9, 30))              # 跨年

    def test_until_fallbacks(self):
        now = 1_800_000_000
        rate = {"windows": {"five_hour": {"utilization": 1.0, "resetsAt": now + 900, "status": "rejected"},
                            "seven_day": {"utilization": 0.5, "resetsAt": now + 99999, "status": "allowed"}}}
        with mock.patch("ccim.usage.cached", lambda: rate):
            self.assertEqual(limits.until("You've hit your limit", now), now + 900)
        with mock.patch("ccim.usage.cached", lambda: None):
            self.assertEqual(limits.until("You've hit your limit", now), now + limits.FALLBACK)

    def test_text(self):
        now = time.time()
        at = time.mktime(time.localtime(now)[:3] + (23, 59, 0, 0, 0, -1))
        self.assertEqual(limits.text(at), "额度用完了，23:59 后自动继续")

    def test_due_staggers(self):
        now = 10_000
        w = [("a", {"until": now - 300, "at": 5}), ("b", {"until": now - 300, "at": 3})]
        self.assertEqual(limits.due(w, 0, now), "b")                     # 先停下的先放
        self.assertIsNone(limits.due(w, now - 30, now))                  # 刚放过一个：隔一会儿
        self.assertIsNone(limits.due(w + [("c", {"until": now + 600, "at": 9})], 0, now))   # 又撞上限的：大家一起等
        self.assertIsNone(limits.due([("a", {"until": now - 10, "at": 1})], 0, now))       # 刚到点：再等一会儿


class FakeChannel:
    live = True
    cards = False
    resumes = True

    def __init__(self):
        self.sent = []

    async def progress(self, cid, snap):
        pass

    async def typing(self, cid, on):
        pass

    async def send_text(self, cid, text, reply_to=None, uid=None):
        self.sent.append(text)


class ChatParkTests(unittest.IsolatedAsyncioTestCase):
    def chat(self, channel):
        saved = {}
        b = SimpleNamespace(channel=channel, saved=lambda cid: saved, save=lambda cid, **kw: saved.update(kw))
        c = object.__new__(Chat)
        c.b, c.chat_id, c.stopping, c.skipping, c.limited, c.parked = b, "w-1", False, False, False, []
        return c, saved

    async def test_limit_parks_task_and_says_chinese(self):
        c, saved = self.chat(FakeChannel())
        task = Task("去做")
        c.current = (task, None)
        t = SimpleNamespace(steps=[], card_id=None, start=time.monotonic(), said=[], live="", text="", retry=None,
                            writing={}, changed=[], created=[], uuid=None, prompt=None, stalled=lambda: None)
        with mock.patch("ccim.limits.until", lambda e: time.time() + 600):
            await c._finish(t, None, "You've hit your 5-hour limit · resets 1:20am (Asia/Shanghai)", None)
        self.assertTrue(c.limited)
        self.assertIn("额度用完了", c.b.channel.sent[-1])
        self.assertIn("后自动继续", c.b.channel.sent[-1])
        self.assertNotIn("hit your", c.b.channel.sent[-1])
        self.assertIn("until", saved["limit"])
        self.assertEqual(c.parked, [task])
        self.assertTrue(task.carried)
        self.assertFalse(task.future.done())
        c.unpark("ok", "做完了")                 # 接着做完的那一轮把结果交过去
        self.assertEqual(task.future.result(), ("ok", "做完了"))

    async def test_stop_clears_waiting(self):
        c, saved = self.chat(FakeChannel())
        task = Task("去做")
        c.parked, saved["limit"] = [task], {"until": 1, "at": 1}
        c.unpark("stopped")
        self.assertIsNone(saved["limit"])
        self.assertEqual(task.future.result()[0], "stopped")

    async def test_other_channels_only_translate(self):
        ch = FakeChannel()
        ch.resumes = False
        c, saved = self.chat(ch)
        c.current = (Task("去做"), None)
        t = SimpleNamespace(steps=[], card_id=None, start=time.monotonic(), said=[], live="", text="", retry=None,
                            writing={}, changed=[], created=[], uuid=None, prompt=None, stalled=lambda: None)
        await c._finish(t, None, "You've hit your 5-hour limit · resets 1:20am (Asia/Shanghai)", None)
        self.assertIn("刷新后可以接着聊", ch.sent[-1])
        self.assertNotIn("limit", saved)
        self.assertEqual(c.parked, [])


class WakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        now = time.time()
        self.meta = {"w-a": {"limit": {"until": now - 200, "at": now - 5000}},
                     "w-b": {"limit": {"until": now - 200, "at": now - 6000}},
                     "w-old": {"limit": {"until": now - 200, "at": now - limits.KEEP - 10}},
                     "w-busy": {"busy": True, "limit": {"until": now - 200, "at": now - 9000}}}
        self.pf = pf = object.__new__(Platform)
        pf.channel = SimpleNamespace(logs=self.logs, _emit=lambda cid, rec: None)
        pf.chats = lambda: self.meta
        pf.meta = lambda cid: self.meta[cid]
        pf.store = SimpleNamespace(write=lambda chats: None)
        self.sub = []
        pf._bridge = lambda cid: SimpleNamespace(chat=lambda c, t: SimpleNamespace(
            busy=False, submit=lambda task: self.sub.append((c, task))))

    def tearDown(self):
        self.dir.cleanup()

    async def test_wakes_one_at_a_time(self):
        at = self.pf.wake_limited(0)
        self.assertTrue(at)
        self.assertEqual(self.sub[0][0], "w-b")              # 先停下的先唤醒；在干活的不管
        self.assertIn("额度用完被打断", self.sub[0][1])
        self.assertNotIn("limit", self.meta["w-b"])
        self.assertNotIn("limit", self.meta["w-old"])        # 等太久的算了
        self.assertIn("额度刷新了", [r.get("text") for r in self.logs.read("w-b")][-1])
        self.assertIsNone(self.pf.wake_limited(at))         # 下一个要隔一会儿
        self.pf.wake_limited(at - limits.STAGGER - 1)
        self.assertEqual(self.sub[-1][0], "w-a")


class DeskPanelTests(unittest.TestCase):
    def test_error_card_waits_and_limit_works(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        logs = Logs(d.name)
        meta = {"w-desk": {"helper": "desk"}, "w-err": {"agent": "dev"}, "w-lim": {"agent": "dev"}, "w-ok": {"agent": "dev"}}
        now = time.time()
        for cid, st in (("w-err", "error"), ("w-lim", "working"), ("w-ok", "done")):
            logs.append("w-desk", {"type": "work", "id": "k-" + cid, "chat": cid, "agent": "dev", "title": cid, "state": st,
                                   "desk": "new", "start": now - 100, **({} if st == "working" else {"done_at": now - 10})})
        logs.append("w-err", {"type": "answer", "text": "出错了", "state": "error", "at": now - 12})
        pf = object.__new__(Platform)
        pf.channel = SimpleNamespace(logs=logs)
        pf.chats = lambda: meta
        pf.works = {"k-w-lim"}
        views = {"w-err": {}, "w-ok": {}, "w-lim": {"limit": {"until": now + 600, "text": "额度用完了，01:20 后自动继续"}}}
        pf.chat_view = lambda cid: dict(views[cid], title=cid)
        with mock.patch("ccim.share.owner_map", lambda pf: {}):
            p = desk.panel(pf)
        self.assertEqual([r["chat"] for r in p["waiting"]], ["w-err"])
        self.assertEqual([r["chat"] for r in p["done"]], ["w-ok"])
        self.assertEqual([(r["chat"], r["line"]) for r in p["working"]], [("w-lim", "额度用完了，01:20 后自动继续")])


class BoardTests(unittest.TestCase):
    def test_waiting_for_quota_is_not_attention(self):
        summ = {"answer_at": 10, "answer_state": "error"}
        self.assertEqual(board.attention({}, summ), "err")
        self.assertIsNone(board.attention({"limit": {"until": 1}}, summ))
        self.assertFalse(board.answered({"limit": {"until": 1}}, summ))


if __name__ == "__main__":
    unittest.main()
