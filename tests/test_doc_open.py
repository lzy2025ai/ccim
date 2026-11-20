"""用 Word 打开对话里的文档（#511）：存过盘下一次发消息时告诉助手，助手自己改的不算。不跑真实对话。"""
import os
import unittest
from unittest.mock import patch

from tests import test_side_mention as base


class DocOpenTests(base.SideMentionTests):                           # 只借它的 setUp

    def test_word_edits_told_once(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        path = os.path.join(self.tmp.name, "方案.docx")
        with open(path, "wb") as f:
            f.write(b"v1")
        pf.channel.logs.append(cid, {"type": "file", "path": path, "name": "方案.docx"})
        with patch("subprocess.run") as run:
            pf.doc_open(cid, path)
        self.assertEqual(run.call_args[0][0], ["open", path])
        self.assertEqual(pf._doc_edits(cid), "")                     # 没存过盘
        os.utime(path, (1, 1))
        self.assertIn(path, pf._doc_edits(cid))
        self.assertEqual(pf._doc_edits(cid), "")                     # 说过一次就不再说

        os.utime(path, (2, 2))                                       # 助手这一轮自己改的
        pf.channel.logs.append(cid, {"type": "answer", "text": "改好了", "changed": [{"path": path}]})
        pf._doc_rebase(cid)
        self.assertEqual(pf._doc_edits(cid), "")

        os.remove(path)
        self.assertEqual(pf._doc_edits(cid), "")
        self.assertNotIn("doc_opened", pf.chats()[cid])

    def test_only_files_in_chat(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        with patch("subprocess.run") as run, self.assertRaises(Exception):
            pf.doc_open(cid, os.path.join(self.tmp.name, "别的.docx"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
