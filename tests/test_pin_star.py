"""置顶和星标合成一个（#347）：老星标转成置顶，排在原来置顶的后面；星标的改动按置顶处理。"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import registry


class PinStarTests(unittest.TestCase):
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
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18980}
        self.pf = platform.Platform(h, entry)
        self.pf.channel = platform.WebChannel(h, entry, None, {"platform": self.pf})
        registry.write_state(h, chats={
            "w-1": {"title": "置顶", "pin": -100.0},
            "w-2": {"title": "星标", "star": True},
            "w-3": {"title": "两个都有", "pin": -50.0, "star": True},
            "w-4": {"title": "归档的星标", "star": True, "archived": True},
        }, marks={"x:a": {"star": True}})

    def test_migrate(self):
        self.pf._stars_to_pins()
        c = self.pf.chats()
        self.assertEqual(c["w-1"]["pin"], -100.0)
        self.assertEqual(c["w-3"]["pin"], -50.0)
        self.assertGreater(c["w-2"]["pin"], -50.0)
        self.assertNotIn("pin", c["w-4"])
        self.assertFalse(any("star" in m for m in c.values()))
        marks = registry.read_state(self.pf.home)["marks"]
        self.assertNotIn("star", marks["x:a"])
        self.assertIsNotNone(marks["x:a"]["pin"])

    def test_old_star_change_is_pin(self):
        self.pf._stars_to_pins()
        self.pf.mark_chat("w-1", {"star": None})
        self.assertNotIn("pin", self.pf.chats()["w-1"])
        self.pf.mark_chat("w-1", {"star": True})
        self.assertIsNotNone(self.pf.chats()["w-1"].get("pin"))


if __name__ == "__main__":
    unittest.main()
