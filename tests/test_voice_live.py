"""语音输入边说边识别、录音留存、识别后整理（#556）。不起真的识别进程：_ask、_wait_ready 换成假的。"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

from ccim import registry, voice


class FakeEngine(voice.Engine):
    def __init__(self):
        super().__init__()
        self.proc = object()
        self.fed = []
        self.busy = False

    def _wait_ready(self, wait):
        return {"preparing": {"state": "preparing"}} if self.busy else None

    def _ask(self, msg, pcm):
        self.fed.append((dict(msg), len(pcm)))
        return f"句{len(self.fed)}"


class ChunkTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.old, registry.HOME = registry.HOME, self.home

    def tearDown(self):
        registry.HOME = self.old

    def path(self, sid):
        return os.path.join(self.home, "voice", "recent", sid + ".pcm")

    def test_chunks_saved_and_fed_once(self):
        e = FakeEngine()
        self.assertEqual(e.chunk("a1", 0, b"\1\0" * 100, False), {"text": "句1"})
        self.assertEqual(e.chunk("a1", 0, b"\1\0" * 100, False), {"text": ""})   # 重发同一块：不重复存、不重复识别
        self.assertEqual(os.path.getsize(self.path("a1")), 200)
        r = e.chunk("a1", 1, b"\2\0" * 50, True)
        self.assertEqual(r, {"text": "句2", "lost": False})
        self.assertEqual(os.path.getsize(self.path("a1")), 300)
        self.assertEqual([n for _, n in e.fed], [200, 100])
        self.assertTrue(e.fed[-1][0]["final"])
        self.assertNotIn("a1", e.sessions)

    def test_not_ready_keeps_audio_then_catches_up(self):
        e = FakeEngine()
        e.busy = True
        self.assertIn("preparing", e.chunk("b1", 0, b"\0\0" * 100, False))
        self.assertEqual(e.fed, [])
        e.busy = False
        e.chunk("b1", 1, b"\0\0" * 100, False)
        self.assertEqual(e.fed[0][1], 400)                  # 前面没交的一起补上

    def test_worker_restarted_marks_lost(self):
        e = FakeEngine()
        e.chunk("c1", 0, b"\0\0" * 10, False)
        e.proc = object()                                     # 识别进程换过
        self.assertTrue(e.chunk("c1", 1, b"\0\0" * 10, True)["lost"])

    def test_ccim_restarted_resumes(self):
        FakeEngine().chunk("d1", 0, b"\0\0" * 100, False)
        e = FakeEngine()                                      # ccim 重启：新的 Engine，录音文件还在
        r = e.chunk("d1", 5, b"\0\0" * 10, True)
        self.assertTrue(r["lost"])
        self.assertEqual(e.fed[0][1], 20)                     # 只交新来的
        self.assertEqual(os.path.getsize(self.path("d1")), 220)

    def test_bad_sid(self):
        with self.assertRaises(ValueError):
            FakeEngine().chunk("../x", 0, b"", False)

    def test_keep_recent(self):
        e = FakeEngine()
        for i in range(voice.KEEP + 3):
            e.chunk(f"r{i}", 0, b"\0\0", True)
            os.utime(self.path(f"r{i}"), (time.time() + i, time.time() + i))
        e.chunk("last", 0, b"\0\0", True)
        left = sorted(os.listdir(os.path.dirname(self.path("x"))))
        self.assertEqual(len(left), voice.KEEP + 1)
        self.assertIn("last.pcm", left)
        self.assertNotIn("r0.pcm", left)


class PolishTest(unittest.TestCase):
    def run_polish(self, text, out=None, err=None):
        async def fake(t):
            if err:
                raise err
            return out
        with mock.patch.object(voice, "_polish", fake):
            return asyncio.run(voice.polish(text))["text"]

    def test_polished(self):
        self.assertEqual(self.run_polish("嗯那个帮我看一下进度吧", "帮我看一下进度。"), "帮我看一下进度。")

    def test_keep_original_when_failed_or_off(self):
        raw = "嗯那个帮我看一下进度吧"
        self.assertEqual(self.run_polish(raw, err=RuntimeError("x")), raw)
        self.assertEqual(self.run_polish(raw, ""), raw)
        self.assertEqual(self.run_polish(raw, "好" * 200), raw)       # 加了一大堆
        self.assertEqual(self.run_polish("好的", "好的。"), "好的")      # 太短不整理

    def test_unwrap(self):
        self.assertEqual(voice._unwrap("<整理后>\n第一段\n\n第二段\n</整理后>"), "第一段\n\n第二段")
        self.assertEqual(voice._unwrap("说明\n<整理后>正文</整理后>"), "正文")
        self.assertEqual(voice._unwrap("正文"), "正文")


if __name__ == "__main__":
    unittest.main()
