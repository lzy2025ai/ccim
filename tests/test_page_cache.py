"""网页允许缓存、侧边对话顺带内容（#490）：首页按内容摘要 304、带版本号的地址直接用缓存；接口照旧不缓存；
总控首页 304 时也重发 cookie；开侧边对话、取侧边对话列表时顺带内容。不读真实登记表。"""
import http.client
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from ccim import hubserver, platform, registry
from tests import test_side_mention as side_base


class PageReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.page = os.path.join(self.tmp.name, "web.html")
        with open(self.page, "w") as f:
            f.write('<script>const PAGE_V = "__CCIM_V__";</script>')
        p = patch.object(platform, "PAGE", self.page)
        p.start()
        self.addCleanup(p.stop)
        platform._PAGE.clear()
        self.addCleanup(platform._PAGE.clear)

    def test_version_etag_and_immutable(self):
        code, data, h = platform.page_reply({}, {})
        self.assertEqual(code, 200)
        v = h["ETag"].strip('"')
        self.assertEqual(len(v), 16)
        self.assertIn(f'PAGE_V = "{v}"'.encode(), data)               # 页面知道自己的版本
        self.assertEqual(h["Cache-Control"], "no-cache")              # 首页每次问，没变就 304
        self.assertEqual(platform.page_reply({"If-None-Match": f'"{v}"'}, {})[0], 304)
        self.assertEqual(platform.page_reply({"If-None-Match": f'W/"{v}"'}, {})[0], 304)   # nginx 压缩后的弱 ETag
        self.assertIn("immutable", platform.page_reply({}, {"v": v})[2]["Cache-Control"])
        self.assertEqual(platform.page_reply({}, {"v": "0" * 16})[2]["Cache-Control"], "no-cache")   # 旧版本号：照常问

    def test_new_version_after_upgrade(self):
        v1 = platform.page_reply({}, {})[2]["ETag"]
        with open(self.page, "a") as f:
            f.write("<p>新版本</p>")
        os.utime(self.page, ns=(1, 2))
        code, data, h = platform.page_reply({"If-None-Match": v1}, {})
        self.assertEqual(code, 200)
        self.assertNotEqual(h["ETag"], v1)
        self.assertIn("新版本".encode(), data)


class HubPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(registry, "HOME", self.tmp.name),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, "registry.json"))):
            p.start()
            self.addCleanup(p.stop)
        platform._PAGE.clear()
        self.hub = hubserver.Hub("https://hub.example")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = srv.server_address[1]

    def get(self, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=dict({"Host": "127.0.0.1"}, **headers))
        r = c.getresponse()
        out = r.status, {k: r.headers.get_all(k) for k in ("Cache-Control", "ETag", "Set-Cookie")}, r.read()
        c.close()
        return out

    def test_page_304_keeps_cookie(self):
        t = self.hub.setup_token()
        d = self.hub.setup_begin(t)
        self.hub.setup_finish(t, "owner-password", hubserver.totp_at(d["secret"], int(time.time()) // 30))
        tok = self.hub.new_session()
        st, h, body = self.get("/", {"Cookie": f"ccim_hub={tok}"})
        self.assertEqual(st, 200)
        self.assertEqual(h["Cache-Control"], ["no-cache"])
        self.assertTrue(any(c.startswith("ccim_hub=") for c in h["Set-Cookie"]))
        st, h, body = self.get("/", {"Cookie": f"ccim_hub={tok}", "If-None-Match": h["ETag"][0]})
        self.assertEqual(st, 304)
        self.assertEqual(body, b"")
        self.assertTrue(any(c.startswith("ccim_hub=") for c in h["Set-Cookie"]))   # 304 也重发（#473）

    def test_login_page_and_api_not_cached(self):
        st, h, _ = self.get("/", {})
        self.assertEqual(h["Cache-Control"], ["no-store"])                     # 没登录：登录页不缓存
        st, h, _ = self.get("/api/state", {"X-CCIM": "1"})
        self.assertEqual(h["Cache-Control"], ["no-store"])


class SideDetailTests(side_base.SideMentionTests):                      # 只借它的 setUp

    def test_side_new_and_list_carry_detail(self):
        pf = self.pf
        main = self.group()
        self.assertNotIn("detail", platform.sides_of(pf, main, True))     # 还没有侧边对话
        r = platform.side_new(pf, main, True)
        self.assertEqual(r["chat"]["id"], r["id"])
        self.assertEqual(r["chat"]["log"], [])
        pf.channel.logs.append(r["id"], {"type": "user", "id": "u9", "text": "它在做什么"})
        s = platform.sides_of(pf, main, True)
        self.assertEqual(s["detail"]["id"], r["id"])
        self.assertEqual([x["text"] for x in s["detail"]["log"]], ["它在做什么"])
        self.assertNotIn("detail", platform.sides_of(pf, main))           # 不要就不带


if __name__ == "__main__":
    unittest.main()
