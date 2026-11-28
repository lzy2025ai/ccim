"""群聊里被 @ 的成员要用户批准：确认卡片也显示在群聊里，在群里点交给子对话处理（#601）。不跑真实对话。"""
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import people, platform


class GroupApproveCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        h = os.path.realpath(self.tmp.name)
        ch = self.ch = platform.WebChannel(h, {"kind": "platform", "channel": "web", "port": 18979}, None, {"platform": None})
        ch.platform = self
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

    def rows(self, cid):
        return [r for c, r in self.emitted if c == cid]

    CARD = {"title": "需要你批准", "color": "orange", "body": "x", "buttons": [{"text": "允许", "value": {"ccim": "approve", "id": "a1"}}]}

    async def test_card_in_group_and_routed_to_sub(self):
        mid = await self.ch.send_card("sub", self.CARD)
        g = self.rows("group")[0]
        self.assertEqual((g["type"], g["id"], g["sub"], g["buttons"]), ("card", mid, "sub", self.CARD["buttons"]))
        self.assertEqual(self.ch.card_owner("group", mid), "sub")
        self.assertEqual(self.ch.card_owner("other", mid), "other")
        await self.ch.update_card(mid, {"title": "已允许", "color": "green"})
        self.assertEqual(self.rows("group")[-1]["title"], "已允许")
        self.assertEqual(self.rows("sub")[-1]["title"], "已允许")

    async def test_other_people_in_group_do_not_see_it(self):
        self.crowd = {"group": ["someone"]}
        mid = await self.ch.send_card("sub", self.CARD)
        await self.ch.update_card(mid, {"title": "已允许"})
        self.assertEqual(self.rows("group"), [])
        self.assertEqual(self.ch.card_owner("group", mid), "group")

    async def test_private_chat_unchanged(self):
        self.turn_of = None
        mid = await self.ch.send_card("sub", self.CARD)
        self.assertEqual([c for c, _ in self.emitted], ["sub"])
        self.assertEqual(self.ch.card_owner("sub", mid), "sub")


if __name__ == "__main__":
    unittest.main()
