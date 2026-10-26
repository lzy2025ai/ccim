"""一轮结束后还在跑的后台任务（#271）：工具调用返回了还没结束的才算后台，结束、断开时清掉，只在任务变了时推送。"""
import types
import unittest

from claude_agent_sdk import SystemMessage, ToolResultBlock, UserMessage

from ccim import session


def sysmsg(subtype, **data):
    return SystemMessage(subtype=subtype, data={"type": "system", "subtype": subtype, **data})


def result(tool):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool, content="ok")])


class BgTaskTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        ch = types.SimpleNamespace(background=lambda cid, tasks, moved=False: self.sent.append((tasks, moved)))
        self.c = session.Chat.__new__(session.Chat)
        self.c.tasks, self.c.detached, self.c.chat_id = {}, set(), "x"
        self.c.b = types.SimpleNamespace(channel=ch)

    def test_foreground_not_shown(self):
        self.c._track(sysmsg("task_started", task_id="a", tool_use_id="t1", description="前台"))
        self.c._track(sysmsg("task_notification", task_id="a", status="completed"))
        self.c._track(result("t1"))
        self.assertEqual(self.c.background(), [])
        self.assertEqual(self.sent, [])

    def test_background_lifecycle(self):
        self.c._track(sysmsg("task_started", task_id="a", tool_use_id="t1", description="跑测试"))
        self.c._track(result("t1"))
        self.assertEqual([t["desc"] for t in self.c.background()], ["跑测试"])
        self.c._track(sysmsg("task_progress", task_id="a", description="Running x", last_tool_name="Bash"))
        self.assertEqual(self.c.background()[0]["desc"], "跑测试")
        self.assertEqual(self.c.background()[0]["step"], "Bash")
        self.assertTrue(self.sent[-1][1])          # 只是走到下一步
        self.c._track(sysmsg("task_updated", task_id="a", patch={"status": "killed"}))
        self.assertEqual(self.c.background(), [])
        self.assertEqual(self.sent[-1], ([], False))
        self.assertEqual(len(self.sent), 3)


if __name__ == "__main__":
    unittest.main()
