"""对话里的文件：浏览器显示得了的 inline，显示不了的（zip、exe 等）attachment。"""
import unittest

from ccim.filesend import disposition


class DispositionTests(unittest.TestCase):
    def test_viewable_inline(self):
        for ct in ("image/png", "video/mp4", "audio/mpeg", "text/plain", "application/pdf"):
            self.assertTrue(disposition("a", ct).startswith("inline;"), ct)

    def test_other_attachment(self):
        for ct in ("application/zip", "application/octet-stream", "application/x-msdownload", "", None):
            self.assertTrue(disposition("a", ct).startswith("attachment;"), ct)

    def test_name_kept(self):
        d = disposition("Mini橙 1.0.1.zip", "application/zip")
        self.assertIn("filename*=UTF-8''Mini%E6%A9%99%201.0.1.zip", d)


if __name__ == "__main__":
    unittest.main()
