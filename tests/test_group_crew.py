"""群聊里被 @ 的成员在干活、答完时，列表和任务面板的数据（#192）；不跑真实对话。"""
import tempfile
import unittest
from types import SimpleNamespace

from ccim import board
from ccim.mentions import Mentions
from ccim.platform import Logs, Platform


class CrewTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        self.pf = object.__new__(Platform)
        self.pf.channel = SimpleNamespace(logs=self.logs, progress_of={})
        self.pf.mentions = Mentions(self.pf)

    def tearDown(self):
        board._SUM.clear()
        self.dir.cleanup()

    def start(self, cid, mid, aid):
        rid = f"{mid}:{aid}"
        self.pf.mentions.running.add(rid)
        self.pf.mentions.where[rid] = (cid, aid, Mentions.sub(cid, aid))
        return rid

    def test_crew_lists_working_members_with_their_step(self):
        m = self.pf.mentions
        self.start("w-a", "1", "ccim")
        self.start("w-a", "2", "ccim")                 # 同一个人排着两件，只算一次
        self.start("w-b", "3", "other")
        self.pf.channel.progress_of[Mentions.sub("w-a", "ccim")] = {"steps": ["读文件", "运行测试"]}
        self.assertEqual(m.crew("w-a"), [{"agent": "ccim", "doing": "运行测试"}])
        self.assertEqual(m.crew("w-b"), [{"agent": "other", "doing": None}])
        m.running.discard("1:ccim"); m.running.discard("2:ccim")
        self.assertEqual(m.crew("w-a"), [])
        self.assertNotIn("1:ccim", m.where)

    def test_member_answer_is_last_by_and_working_hides_attention(self):
        cid = "w-g"
        self.logs.append(cid, {"type": "user", "text": "@ccim 看下"})
        self.logs.append(cid, {"type": "answer", "state": "done", "text": "群主说的"})
        self.logs.append(cid, {"type": "mention", "id": "9:ccim", "agent": "ccim", "state": "done", "text": "成员说的", "done_at": 5})
        marks = self.pf._board_marks(cid, {})
        self.assertEqual(marks["last_by"], "ccim")
        self.assertEqual(marks["attention"], "done")
        self.start(cid, "10", "ccim")
        marks = self.pf._board_marks(cid, {})
        self.assertIsNone(marks["attention"])
        self.assertEqual(marks["crew"], [{"agent": "ccim", "doing": None}])

    def test_main_answer_after_member_resets_last_by(self):
        cid = "w-h"
        self.logs.append(cid, {"type": "mention", "id": "1:ccim", "agent": "ccim", "state": "done", "text": "成员说的"})
        self.logs.append(cid, {"type": "answer", "state": "done", "text": "群主说的"})
        self.assertIsNone(self.pf._board_marks(cid, {})["last_by"])


if __name__ == "__main__":
    unittest.main()
