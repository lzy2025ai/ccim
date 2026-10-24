"""面板标题使用用户正文，不将引用当成新请求；不读取真实对话。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ccim import board
from ccim.platform import Logs, Platform


class AskPreviewTests(unittest.TestCase):
    def test_leading_quotes_do_not_hide_the_request(self):
        examples = [
            ("> 报价按文档计算\n\n把这段改成书面表达", "把这段改成书面表达"),
            ("> 第一段\n> 第二行\n\n> 另一段\n\n好的，按这个做", "好的，按这个做"),
            ("\r\n  > 引用\r\n >> 嵌套引用\r\n\r\n请解释区别", "请解释区别"),
            ("> " + "很长的引用" * 100 + "\n\n实际请求", "实际请求"),
        ]
        for text, expected in examples:
            with self.subTest(text=text[:50]):
                self.assertEqual(board.ask_preview(text), expected)

    def test_quote_only_returns_empty_for_existing_title_fallback(self):
        for text in (None, "", " \n", "> 引用", "> 引用\n\n> 另一段\n"):
            with self.subTest(text=text):
                self.assertEqual(board.ask_preview(text), "")

    def test_body_and_existing_display_rules_are_preserved(self):
        self.assertEqual(board.ask_preview("  请检查\n\n这个问题  "), "请检查 这个问题")
        self.assertEqual(board.ask_preview("请解释下面这句\n> 示例"), "请解释下面这句 > 示例")
        self.assertEqual(board.ask_preview("```\n> 代码内容\n```"), "``` > 代码内容 ```")
        self.assertEqual(board.ask_preview("> 引用\n\n/queue 帮我检查"), "/queue 帮我检查")
        self.assertEqual(board.ask_preview("正文" * 100), ("正文" * 100)[:120])

    def test_historical_log_and_cached_summary_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            logs = Logs(folder)
            cid = "quote-preview-regression"
            original = "> " + "历史引用" * 100 + "\n\n这是这一轮真正的请求"
            logs.append(cid, {"type": "user", "text": original})
            logs.append(cid, {"type": "answer", "state": "done", "at": 2})
            pf = object.__new__(Platform)
            pf.channel = SimpleNamespace(logs=logs)
            pf.mentions = SimpleNamespace(crew=lambda cid: [])
            path = Path(logs._f(cid))
            before = path.read_bytes()
            try:
                board.summary(logs, cid)  # 已存在的缓存仍保存完整原文
                for _ in range(2):
                    marks = pf._board_marks(cid, {})
                    self.assertEqual(marks["ask"], "这是这一轮真正的请求")
                    self.assertEqual(marks["attention"], "done")
                    self.assertTrue(marks["unread"])
                self.assertEqual(board.summary(logs, cid)["last_user"], original)
                self.assertEqual(path.read_bytes(), before)
            finally:
                board._SUM.pop(cid, None)


if __name__ == "__main__":
    unittest.main()
