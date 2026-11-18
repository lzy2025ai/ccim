"""Word 预览（#495）：图片不随正文发，网页按名字单独取；大文件也能预览，超大的纯文本给下载。"""
import asyncio
import http.client
import json
import os
import tempfile
import threading
import unittest
import zipfile
from urllib.parse import quote
from unittest.mock import patch

from ccim import agents, docs, registry, share

DOC = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body>
<w:p><w:r><w:t>你好</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="476250"/><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
</w:body></w:document>"""
RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="image" Target="media/a.png"/></Relationships>"""


def make_docx(path, pad=0):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", DOC)
        z.writestr("word/_rels/document.xml.rels", RELS)
        z.writestr("word/media/a.png", b"\x89PNG" + b"x" * 10)
        if pad:
            z.writestr("word/media/big.bin", os.urandom(pad))


class DocxPartTests(unittest.TestCase):
    def test_view_lists_parts(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.docx")
            make_docx(p, pad=docs.MAX_DOC + 1)          # 超过 20MB 也照样预览
            out = docs.view(p)
            self.assertEqual(out["kind"], "docx")
            self.assertEqual(out["parts"], ["word/media/a.png"])
            self.assertIn('<img data-p="0" width="100" height="50" alt="">', out["html"])
            self.assertIn('<img data-p="0" width="1" height="1" alt="">', out["html"])
            self.assertNotIn("data:", out["html"])
            self.assertEqual(docs.docx_part(p, "word/media/a.png")[1], "image/png")
            for bad in ("word/document.xml", "word/../x.png", "other/a.png", "word/media/none.png"):
                with self.assertRaises(docs.Problem):
                    docs.docx_part(p, bad)

    def test_big_text_offers_download(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.md")
            with open(p, "wb") as f:
                f.truncate(docs.MAX_DOC + 1)
            self.assertEqual(docs.view(p)["kind"], "other")


class DocxHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        ps = [patch.object(registry, "HOME", os.path.join(home, ".ccim")),
              patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
              patch.dict(os.environ, {"HOME": home})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        from ccim.peers import Peers
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18978}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        pf.external_chats = lambda *a, **k: []
        self.server = pf.serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.agent = agents.create(name="助手")["id"]
        self.doc = os.path.join(home, "a.docx")
        make_docx(self.doc)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path, headers={"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        out = r.status, r.getheader("Content-Type"), r.read()
        c.close()
        return out

    def test_part_served_only_for_chat_files(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("POST", "/api/chat/new", body=json.dumps({"agent": self.agent}).encode(),
                  headers={"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"})
        cid = json.loads(c.getresponse().read())["id"]
        c.close()
        q = f"chat={cid}&path={quote(self.doc)}"
        self.assertEqual(self.get(f"/api/file?{q}&part=word/media/a.png")[0], 404)   # 对话里没有这个文件
        self.pf.channel.logs.append(cid, {"type": "file", "path": self.doc})
        st, _, body = self.get(f"/api/doc?{q}")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["parts"], ["word/media/a.png"])
        st, ctype, data = self.get(f"/api/file?{q}&part=word/media/a.png")
        self.assertEqual((st, ctype), (200, "image/png"))
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(self.get(f"/api/file?{q}&part=word/document.xml")[0], 404)


if __name__ == "__main__":
    unittest.main()
