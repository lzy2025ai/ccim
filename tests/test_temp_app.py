"""临时对话来自哪个应用、应用说明（#550）：记下来源、列表里显示应用名、说明进系统提示；不碰真实主目录。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import registry
from ccim.session import role_note


class TempAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        self.P = platform
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18979}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)

    def test_app_recorded_and_shown_as_source(self):
        cid = self.pf.new_chat(temp={"base": "codex", "app": {"id": "ai-browser", "name": "AI Browser"}, "note": "你是浏览器助理。"})
        t = self.pf.meta(cid)["temp"]
        self.assertEqual(t["app"], {"id": "ai-browser", "name": "AI Browser"})
        self.assertEqual(t["note"], "你是浏览器助理。")
        v = self.pf.chat_view(cid)
        self.assertEqual(v["app"], {"id": "ai-browser", "name": "AI Browser"})
        self.assertEqual(v["source"], "AI Browser")

    def test_note_goes_into_role(self):
        cid = self.pf.new_chat(temp={"app": {"id": "ai-browser", "name": "AI Browser"}, "note": "你是浏览器助理。"})
        b = self.pf._bridge(cid)
        note = role_note(b.entry)
        self.assertIn("「AI Browser」", note)
        self.assertIn("你是浏览器助理。", note)

    def test_plain_temp_unchanged(self):
        cid = self.pf.new_chat(temp={"base": "claude", "note": "没有应用就不收"})
        t = self.pf.meta(cid)["temp"]
        self.assertNotIn("app", t)
        self.assertNotIn("note", t)
        v = self.pf.chat_view(cid)
        self.assertEqual(v["source"], "网页")
        self.assertIsNone(v["app"])
        self.assertEqual(role_note(self.pf._bridge(cid).entry), "")

    def test_bad_app_ignored_and_note_capped(self):
        self.assertIsNone(self.P._app_of({"id": "../", "name": "x"}))
        self.assertIsNone(self.P._app_of("ai-browser"))
        self.assertEqual(self.P._app_of({"id": "a b/c", "name": "  AI   Browser "}), {"id": "abc", "name": "AI Browser"})
        cid = self.pf.new_chat(temp={"app": {"id": "x", "name": "X"}, "note": "长" * 30000})
        self.assertEqual(len(self.pf.meta(cid)["temp"]["note"]), self.P.APP_NOTE_MAX)


if __name__ == "__main__":
    unittest.main()
