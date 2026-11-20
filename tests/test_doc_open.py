"""用 Word 打开对话里的文档（#511）：存过盘下一次发消息时告诉助手，助手自己改的不算。不跑真实对话。"""
import os
import unittest
from unittest.mock import patch

from tests import test_side_mention as base


class DocOpenTests(base.SideMentionTests):                           # 只借它的 setUp

    def test_word_edits_told_once(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        path = os.path.join(self.tmp.name, "方案.md")
        with open(path, "w") as f:
            f.write("v1")
        pf.channel.logs.append(cid, {"type": "file", "path": path, "name": "方案.md",
                                     "snap": __import__("ccim.docs", fromlist=["x"]).snapshot(pf.channel.logs.folder, cid, path)})
        with patch("subprocess.run") as run:
            pf.doc_open(cid, path)
        self.assertEqual(run.call_args[0][0], ["open", path])
        self.assertEqual(pf._doc_edits(cid), "")                     # 没存过盘
        with open(path, "w") as f:
            f.write("v2 我改的")
        os.utime(path, (1, 1))
        vs = pf.doc_view(cid, path)["versions"]                      # 打开面板就看得到这一版
        self.assertEqual([(v["by"], v["via"]) for v in vs], [("agent", None), ("me", "word")])
        self.assertIn(path, pf._doc_edits(cid))
        self.assertEqual(pf._doc_edits(cid), "")                     # 说过一次就不再说
        self.assertIn("v1", open(pf.doc_versions(cid, path)[0]["snap"]).read())   # 改之前的还在

        with open(path, "w") as f:                                   # 助手这一轮自己改的
            f.write("v3 助手改的")
        os.utime(path, (2, 2))
        pf.channel.logs.append(cid, {"type": "answer", "text": "改好了", "changed": [{"path": path}]})
        pf._doc_agent_vers(cid)
        self.assertEqual(pf._doc_edits(cid), "")
        vs = pf.doc_view(cid, path)["versions"]
        self.assertEqual([(v["by"], v["via"]) for v in vs], [("agent", None), ("me", "word"), ("agent", "edit")])
        pf.channel.logs.append(cid, {"type": "answer", "text": "没再改", "changed": [{"path": path}]})
        pf._doc_agent_vers(cid)                                      # 内容没变：不再多一版
        self.assertEqual(len(pf.doc_versions(cid, path)), 3)

        os.remove(path)
        self.assertEqual(pf._doc_edits(cid), "")
        self.assertNotIn("doc_opened", pf.chats()[cid])

    def test_agent_told_while_open_in_word(self):                 # #515
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        path = os.path.join(self.tmp.name, "HoYgent_二期页面功能清单.docx")
        with open(path, "wb") as f:
            f.write(b"v1")
        pf.channel.logs.append(cid, {"type": "file", "path": path, "name": os.path.basename(path)})
        with patch("subprocess.run"):
            pf.doc_open(cid, path)
        self.assertEqual(pf._doc_edits(cid), "")                     # 还没生成锁文件
        lock = os.path.join(self.tmp.name, "~$Ygent_二期页面功能清单.docx")   # 名字长：开头两个字换成 ~$
        open(lock, "w").close()
        self.assertIn("正在 Word 里编辑", pf._doc_edits(cid))
        self.assertIn("正在 Word 里编辑", pf._doc_edits(cid))         # 开着就每次都说
        with open(path, "wb") as f:                                  # 助手还是改了
            f.write(b"v2")
        pf.channel.logs.append(cid, {"type": "answer", "text": "改好了", "changed": [{"path": path}]})
        pf._doc_agent_vers(cid)
        self.assertTrue(any(r.get("type") == "note" and "还在 Word 里开着" in r["text"] for r in pf.channel.logs.read(cid)))
        os.remove(lock)                                              # 关掉 Word
        self.assertNotIn("正在 Word 里编辑", pf._doc_edits(cid))

    def test_only_files_in_chat(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        with patch("subprocess.run") as run, self.assertRaises(Exception):
            pf.doc_open(cid, os.path.join(self.tmp.name, "别的.docx"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
