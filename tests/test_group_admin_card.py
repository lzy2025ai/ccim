"""群聊里被 @ 的成员要管理员权限：卡片也显示在群聊里（#428）。不跑真实对话。"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import people, platform


class GroupAdminCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        h = os.path.realpath(self.tmp.name)
        ch = self.ch = platform.WebChannel(h, {"kind": "platform", "channel": "web", "port": 18979}, None, {"platform": None})
        ch.platform = self
        self.loop = None
        self.turn_of = "group"
        self.emitted = []
        ch._emit = lambda cid, rec: self.emitted.append((cid, rec))
        ch.group_turn = lambda cid: self.turn_of
        ch.logs = self
        self.crowd = {}
        p = patch.object(people, "visible_map", lambda pf: self.crowd)
        p.start()
        self.addCleanup(p.stop)

    def append(self, cid, rec):               # 代替 logs.append
        return rec

    async def ask(self):
        self.loop = asyncio.get_running_loop()
        t = asyncio.create_task(self.ch.ask_secret("sub", "ls", "看一下", 5))
        await asyncio.sleep(0)
        return t

    def rows(self, cid):
        return [r for c, r in self.emitted if c == cid]

    async def test_card_shows_in_group_and_answer_updates_both(self):
        t = await self.ask()
        card = self.rows("group")[0]
        self.assertEqual((card["type"], card["state"], card["sub"], card["command"]), ("secret", "pending", "sub", "ls"))
        self.assertEqual(card["id"], self.rows("sub")[0]["id"])
        self.assertTrue(self.ch.answer_secret(card["id"], "pw"))
        self.assertEqual(await t, "pw")
        self.assertEqual(self.rows("group")[-1]["state"], "done")
        self.assertEqual(self.rows("sub")[-1]["state"], "done")

    async def test_other_people_in_group_do_not_see_it(self):
        self.crowd = {"group": ["someone"]}
        t = await self.ask()
        self.assertEqual(self.rows("group"), [])
        self.assertTrue(self.ch.answer_secret(self.rows("sub")[0]["id"], None))
        self.assertIsNone(await t)
        self.assertEqual(self.rows("group"), [])

    async def test_private_chat_unchanged(self):
        self.turn_of = None
        t = await self.ask()
        self.assertEqual(self.rows("group"), [])
        self.ch.answer_secret(self.rows("sub")[0]["id"], None)
        await t


if __name__ == "__main__":
    unittest.main()
