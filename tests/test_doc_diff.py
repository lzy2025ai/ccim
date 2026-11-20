"""文档新版本标出改了哪些内容（#513）：按段比，改了几段、新增几段、删了几段，在哪几个标题下面。"""
import asyncio
import os
import tempfile
import unittest
import zipfile

from ccim import docs
from tests import test_side_mention as base


def write_docx(path, paras):
    body = "".join(('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if h else "<w:p>") + f"<w:r><w:t>{t}</w:t></w:r></w:p>"
                   for h, t in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body>{body}</w:body></w:document>")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)

    def f(self, name, text):
        p = os.path.join(self.d.name, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_md(self):
        a = self.f("a.md", "# 方案\n\n## 一、背景\n\n原来的背景\n\n## 二、报价\n\n- 一期 1 万\n- 二期 2 万\n\n删掉的这段话很长很长\n")
        b = self.f("b.md", "# 方案\n\n## 一、背景\n\n原来的背景，改了一点\n\n## 二、报价\n\n- 一期 1 万\n- 二期 2 万\n- 三期 3 万\n")
        self.assertEqual(docs.change_summary(a, b), {"mod": 1, "add": 1, "del": 1, "where": ["一、背景", "二、报价"], "more": False})
        self.assertEqual(docs.change_summary(a, a), {})

    def test_docx(self):
        x, y = os.path.join(self.d.name, "x.docx"), os.path.join(self.d.name, "y.docx")
        write_docx(x, [(1, "页面清单"), (0, "首页"), (0, "详情页")])
        write_docx(y, [(1, "页面清单"), (0, "首页（改）"), (0, "详情页"), (0, "新页面")])
        self.assertEqual(docs.change_summary(x, y), {"mod": 1, "add": 1, "del": 0, "where": ["页面清单"], "more": False})

    def test_other_kinds(self):
        self.assertIsNone(docs.change_summary(self.f("a.txt", "1"), self.f("b.txt", "2")))


class SendTests(base.SideMentionTests):                              # 只借它的 setUp

    def test_second_send_has_diff(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写方案")
        path = os.path.join(self.tmp.name, "方案.md")
        with open(path, "w") as f:
            f.write("# 方案\n\n第一段\n")
        asyncio.run(pf.channel.send_file(cid, path))
        self.assertNotIn("diff", pf.channel.logs.read(cid)[-1])      # 第一次发：没得比
        with open(path, "w") as f:
            f.write("# 方案\n\n第一段\n\n第二段\n")
        asyncio.run(pf.channel.send_file(cid, path))
        self.assertEqual(pf.channel.logs.read(cid)[-1]["diff"], {"mod": 0, "add": 1, "del": 0, "where": ["方案"], "more": False})


if __name__ == "__main__":
    unittest.main()
