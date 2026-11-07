"""Codex 底座的额度和上下文（#436）：用假的会话记录，不联网。"""
import asyncio, json, os, tempfile, time, unittest
from types import SimpleNamespace
from unittest import mock

from ccim import commands, usage
from ccim.codex import CodexChat


def event(used=27819, window=475000, five=16.0, week=3.0, resets=None):
    resets = resets or time.time() + 3600
    return {"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"last_token_usage": {"total_tokens": used}, "total_token_usage": {"total_tokens": used * 3},
                 "model_context_window": window},
        "rate_limits": {"limit_id": "codex",
                        "primary": {"used_percent": five, "window_minutes": 300, "resets_at": resets},
                        "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": resets}}}}


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        p = mock.patch.dict(os.environ, {"HOME": d.name})
        p.start()
        self.addCleanup(p.stop)

    def rollout(self, thread, lines, day="2026/11/07", mtime=None):
        folder = os.path.join(self.home, ".codex", "sessions", day)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"rollout-2026-11-07T10-00-00-{thread}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for x in lines:
                f.write((x if isinstance(x, str) else json.dumps(x)) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path


class Context(Base):
    def test_latest_request(self):
        self.rollout("t1", [event(used=1000), event(used=47500), {"type": "response_item", "payload": {}}])
        c = usage.codex_context("t1")
        self.assertEqual((c["used"], c["max"], c["pct"]), (47500, 475000, 10.0))

    def test_skips_event_without_info(self):
        e = event(used=2000)
        self.rollout("t1", [e, {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": None}}])
        self.assertEqual(usage.codex_context("t1")["used"], 2000)

    def test_missing_or_broken(self):
        self.assertIsNone(usage.codex_context("nope"))
        self.rollout("t2", ['{"token_count" broken', {"type": "event_msg", "payload": {"type": "token_count"}}])
        self.assertIsNone(usage.codex_context("t2"))


class Limits(Base):
    def test_newest_file_wins_even_in_old_folder(self):
        now = time.time()
        self.rollout("new-day", [event(five=1.0)], day="2026/11/07", mtime=now - 3000)
        self.rollout("old-day", [event(five=33.0)], day="2026/10/01", mtime=now - 10)
        self.assertEqual(usage.codex_status(), "5 小时已用 33% · 每周已用 3%")

    def test_windows_by_length(self):
        e = event()
        r = e["payload"]["rate_limits"]
        r["primary"], r["secondary"] = r["secondary"], r["primary"]
        self.rollout("t", [e])
        self.assertEqual(usage.codex_status(), "5 小时已用 16% · 每周已用 3%")

    def test_report(self):
        self.rollout("t", [event()])
        text = usage.codex_report()
        self.assertIn("**5 小时额度**：已用 16%", text)
        self.assertIn("**每周额度**：已用 3%", text)
        self.assertNotIn("的数据", text)

    def test_old_data_and_past_reset(self):
        self.rollout("t", [event(resets=time.time() - 60)], mtime=time.time() - 7200)
        text = usage.codex_report()
        self.assertIn("重置", text)
        self.assertNotIn("已用", text)
        self.assertIn("这是2 小时前的数据。", text)

    def test_nothing(self):
        self.assertIsNone(usage.codex_status())
        self.assertIn("查不到", usage.codex_report())
        self.rollout("api-key", [{"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": None}}])
        self.assertIsNone(usage.codex_limits())

    def test_no_claude_numbers(self):
        with mock.patch.object(usage, "cached", return_value={"windows": {"five_hour": {"utilization": 0.5}}, "at": time.time()}):
            self.assertIsNone(usage.codex_status())
            self.assertIsNone(asyncio.run(commands._quota("/tmp", "dsh")))


class ChatSide(Base):
    def test_context_saved_and_pushed(self):
        self.rollout("t1", [event(used=47500)])
        saved, pushed = {}, []
        chat = CodexChat.__new__(CodexChat)
        chat.chat_id, chat.session_id = "c1", "t1"
        chat.b = SimpleNamespace(save=lambda cid, **k: saved.update(k),
                                 channel=SimpleNamespace(context=lambda cid, c: pushed.append(c)))
        asyncio.run(chat._context())
        self.assertEqual(saved["ctx"]["pct"], 10.0)
        self.assertEqual(pushed, [saved["ctx"]])


if __name__ == "__main__":
    unittest.main()
