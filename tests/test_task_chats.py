"""主对话派任务对话（#586）：主对话能用 start_work、做完交回（署名、排队、合成一轮、防来回）、/api/chat/send 的 from_chat、
侧边栏按派出关系挂在主对话下面。不跑真实对话。"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ccim import platform as platform_mod
from ccim import session
from ccim.platform import Logs, Platform, Problem

HTML = os.path.join(os.path.dirname(__file__), "..", "ccim", "web.html")


class FakeChat:
    """主循环里的 Chat：只记排进来的活，不跑。"""

    def __init__(self):
        self.queue, self.current = [], None

    def submit(self, task):
        self.queue.append(task)


class TaskChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        self.meta = {"w-main": {"agent": "dev", "title": "主对话"},
                     "w-other": {"agent": "writer", "title": "写文档"},
                     "w-g": {"agent": "dev", "members": ["writer"], "title": "群"},
                     "m-w-g-writer": {"agent": "writer", "mention_of": "w-g"},
                     "w-remote": {"agent": "dev", "title": "别处派来的", "remote_project": True}}
        self.agents = {"dev": {"name": "开发"}, "writer": {"name": "写作"}}
        self.pf = pf = object.__new__(Platform)
        self.emitted = []
        pf.channel = SimpleNamespace(logs=self.logs, progress_of={}, _emit=lambda cid, rec: self.emitted.append((cid, rec)))
        pf.chats = lambda: self.meta
        pf.meta = lambda cid: self.meta.get(cid) or {}
        pf.store = SimpleNamespace(write=lambda chats: None)
        pf.background, pf.works, pf.inbox, pf.held = set(), set(), {}, {}
        self.reported = []
        pf.mentions = SimpleNamespace(report=lambda *a: self.reported.append(a))
        self.chat_objs = {}

        def bridge(cid):
            c = self.chat_objs.setdefault(cid, FakeChat())
            return SimpleNamespace(chat=lambda c_, t: c, chats={cid: c})
        pf._bridge = bridge
        n = iter(range(100))

        def new_chat(agent=None, title=None, parent=None, **kw):
            cid = f"w-new{next(n)}"
            self.meta[cid] = {"agent": agent, "title": title, **({"parent": parent} if parent else {})}
            return cid
        pf.new_chat = new_chat
        pf.chat_view = lambda cid: {"id": cid, "title": self.meta[cid].get("title")}
        self.p = [mock.patch("ccim.agents.all_agents", lambda: self.agents),
                  mock.patch("ccim.agents.get", lambda aid: dict(self.agents[aid], id=aid) if aid in self.agents else None),
                  mock.patch("ccim.agents.cwd", lambda a: "/tmp"),
                  mock.patch("ccim.share.owner_map", lambda pf: {})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.dir.cleanup()

    def queued(self, cid):
        return self.chat_objs.setdefault(cid, FakeChat()).queue

    def reports(self, cid):
        return [r for r in self.logs.read(cid) if r.get("type") == "report"]

    # ----- 谁能用 start_work -----

    def fake_session(self, cid, entry=None):
        return SimpleNamespace(chat_id=cid, chat_type="p2p", b=SimpleNamespace(entry=entry or {}), _guest=lambda: None)

    def test_main_chat_can_work(self):
        self.assertTrue(session._can_work(self.fake_session("w-main"), self.pf))
        self.assertTrue(session._can_work(self.fake_session("w-g"), self.pf))        # 它当群主的群聊

    def test_who_cannot(self):
        self.meta["w-task"] = {"agent": "dev", "title": "任务", "dispatched_from": "w-main"}
        self.assertFalse(session._can_work(self.fake_session("w-task"), self.pf))     # 任务对话不再往下派
        self.assertFalse(session._can_work(self.fake_session("w-remote"), self.pf))   # 别的电脑派来的
        self.assertFalse(session._can_work(self.fake_session("w-main", {"guest": "acct1"}), self.pf))   # 访客
        self.assertFalse(session._can_work(self.fake_session("w-main", {"lent": True}), self.pf))
        self.assertFalse(session._can_work(self.fake_session("w-main", {"helper": "desk"}), self.pf))
        self.assertFalse(session._can_work(self.fake_session("w-main", {"side": True}), self.pf))       # 侧边对话

    async def test_tool_limit_and_depth(self):
        chat = self.fake_session("w-main")
        chat.turn = SimpleNamespace(works=0)
        chat.current = None
        tool = session._start_work(chat, self.pf)
        self.assertIn("agent", tool.input_schema["properties"])
        for i in range(session.MAX_WORKS):
            r = await tool.handler({"text": f"第 {i} 件", "title": f"任务{i}"})
            self.assertNotIn("is_error", r)
        r = await tool.handler({"text": "再来一件"})
        self.assertTrue(r.get("is_error"))
        self.assertEqual(chat.turn.works, session.MAX_WORKS)
        deep = session.Task("x")
        deep.depth = session.MAX_DEPTH
        chat.turn, chat.current = SimpleNamespace(works=0), (deep, None)
        self.assertTrue((await tool.handler({"text": "再往下派"})).get("is_error"))

    # ----- 派出去、交回 -----

    async def test_work_from_main_and_hand_back(self):
        cid, title = self.pf.work("w-main", "改登录页，做完开 PR", "改登录页", depth=1)
        self.assertEqual(self.meta[cid]["dispatched_from"], "w-main")
        self.assertEqual(self.meta[cid]["parent"], "w-main")                # 同一个 Agent：挂在下面
        card = [r for r in self.logs.read("w-main") if r.get("type") == "work"][-1]
        self.assertTrue(card["task"] and card["state"] == "working" and card["chat"] == cid)
        task = self.queued(cid)[0]
        self.assertIn("改登录页，做完开 PR", task)
        self.assertEqual(task.depth, 1)
        self.logs.append(cid, {"type": "answer", "text": "PR #12 开好了"})
        task.finish("ok", "PR #12 开好了，**验收**：登录页")
        await asyncio.sleep(0.05)
        cards = [r for r in self.logs.read("w-main") if r.get("type") == "work"]
        self.assertEqual(cards[-1]["state"], "done")
        self.assertFalse(self.reported)                                     # 主对话不走 Mentions.report
        self.assertEqual(self.reports("w-main"), [])                        # 记录等那一轮开始时才写
        back = self.queued("w-main")[0]
        t = back.on_start()
        rep = self.reports("w-main")[0]
        self.assertEqual((rep["agent"], rep["chat"], rep["title"], rep["task"]), ("dev", cid, "改登录页", True))
        self.assertIn("**验收**", rep["text"])                               # 原文，网页按 Markdown 画
        self.assertIn("PR #12", t)
        self.assertIn("需要用户拍板", t)
        self.assertEqual(t.depth, 1)
        self.assertIs(t.future, back.future)

    async def test_work_for_another_agent(self):
        cid, _ = self.pf.work("w-main", "写说明文档", "说明文档", agent="writer", depth=1)
        self.assertEqual(self.meta[cid]["agent"], "writer")
        self.assertEqual(self.meta[cid]["dispatched_from"], "w-main")
        self.assertNotIn("parent", self.meta[cid])                          # 不是同一个 Agent：不写 parent，前端按 dispatched_from 挂
        rows = platform_mod._agent_chats(_ViewPf(self.pf), "dev")
        hit = [r for r in rows if r["id"] == cid]
        self.assertTrue(hit and hit[0]["task_of"] == "dev")

    async def test_busy_queue_merges(self):
        for i in range(3):
            self.pf.hand_back("w-main", {"type": "report", "chat": f"w-t{i}", "agent": "dev", "title": f"任务{i}", "task": True},
                              f"结果{i}", 1)
        self.assertEqual(len(self.queued("w-main")), 1)                     # 排着的只有一轮
        t = self.queued("w-main")[0].on_start()
        self.assertEqual(len(self.reports("w-main")), 3)
        self.assertIn("有 3 件", t)
        for i in range(3):
            self.assertIn(f"结果{i}", t)
        # 那一轮开始以后再到的：另起一轮
        self.pf.hand_back("w-main", {"type": "report", "chat": "w-t9", "agent": "dev", "title": "任务9", "task": True}, "结果9", 1)
        self.assertEqual(len(self.queued("w-main")), 2)

    async def test_stopped_while_queued(self):
        self.pf.hand_back("w-main", {"type": "report", "chat": "w-t", "agent": "dev", "title": "任务", "task": True}, "结果", 1)
        self.queued("w-main")[0].finish("stopped")                          # /stop 清掉了队列
        await asyncio.sleep(0)
        self.assertEqual(len(self.reports("w-main")), 1)                    # 记录照样写上
        self.assertIn("结果", self.pf.held_note("w-main"))                  # 用户下次开口时带上
        self.pf.hand_back("w-main", {"type": "report", "chat": "w-t", "agent": "dev", "title": "任务", "task": True}, "又一个", 1)
        self.assertEqual(len(self.queued("w-main")), 2)                     # 不会卡在已经停掉的那一轮上

    async def test_too_deep_only_recorded(self):
        self.pf.hand_back("w-main", {"type": "report", "chat": "w-t", "agent": "dev", "title": "任务"}, "来回太多", session.MAX_DEPTH + 1)
        self.assertEqual(self.queued("w-main"), [])
        self.assertEqual(len(self.reports("w-main")), 1)
        self.assertIn("来回太多", self.pf.held_note("w-main"))
        self.assertEqual(self.pf.held_note("w-main"), "")                   # 只带一次

    # ----- from_chat -----

    async def test_from_chat_display_and_depth(self):
        self.meta["w-task"] = {"agent": "dev", "title": "任务甲", "dispatched_from": "w-main"}
        self.pf.message_from("w-task", "w-main", "PR #34 开好了")
        rep_task = self.queued("w-main")[0]
        self.assertEqual(rep_task.depth, 1)                                 # 发送方没在跑：算第一层
        self.queued("w-main")[0].on_start()
        rep = self.reports("w-main")[0]
        self.assertEqual((rep["agent"], rep["chat"], rep["title"], rep["text"], rep.get("task")),
                         ("dev", "w-task", "任务甲", "PR #34 开好了", True))
        self.assertFalse([r for r in self.logs.read("w-main") if r.get("type") == "user"])   # 不当成用户说的
        # 来回：发送方这一轮本身就是交回触发的第 2 层 → 再发过去只记下
        cur = session.Task("x")
        cur.depth = session.MAX_DEPTH
        self.chat_objs["w-main"].current = (cur, None)
        self.pf.message_from("w-main", "w-task", "再改一下")
        self.assertEqual(self.queued("w-task"), [])
        self.assertTrue(self.pf.held.get("w-task"))

    async def test_from_chat_checks(self):
        with self.assertRaises(Problem):
            self.pf.message_from("w-nope", "w-main", "hi")                  # 发送方不存在
        with self.assertRaises(Problem):
            self.pf.message_from("w-remote", "w-main", "hi")                # 不在这台电脑上
        with self.assertRaises(Problem):
            self.pf.message_from("w-main", "w-main", "hi")                  # 发给自己
        with self.assertRaises(Problem):
            self.pf.message_from("w-main", "w-nope", "hi")                  # 收的不存在
        with mock.patch("ccim.share.owner_map", lambda pf: {"w-main": "guest1"}):
            with self.assertRaises(Problem):
                self.pf.message_from("w-main", "w-other", "hi")             # 这个账号看不到发送方
        with self.assertRaises(Problem):
            platform_mod.send(self.pf, "w-main", "hi", [{"key": "/tmp/x"}], from_chat="w-other")   # 只能发文字

    async def test_from_chat_to_fresh_chat_hangs_under(self):
        self.meta["w-fresh"] = {"agent": "dev", "title": "【任务】改登录"}
        self.pf.message_from("w-main", "w-fresh", "去改登录页")
        self.assertEqual(self.meta["w-fresh"]["dispatched_from"], "w-main")
        self.meta["w-fresh"]["title"] = "登录页改好了"                      # 改名不影响
        self.assertEqual(self.meta["w-fresh"]["dispatched_from"], "w-main")


class _ViewPf:
    """_agent_chats 要的 chat_view：带上 dispatched_from。"""

    def __init__(self, pf):
        self.pf = pf

    def chats(self):
        return self.pf.chats()

    def chat_view(self, cid):
        m = self.pf.chats()[cid]
        return {"id": cid, "title": m.get("title"), "agent": m.get("agent"),
                "dispatched_from": {"id": m["dispatched_from"]} if m.get("dispatched_from") else None,
                "group": {"title": "群"}}


def tree(rows):
    """跑 web.html 里的 hangTasks + chatTree：返回 父对话 → [挂在下面的]。"""
    src = open(HTML, encoding="utf8").read()
    defs = [re.search(p, src, re.S | re.M).group(0) for p in (r"^function hangTasks\(rows0\)\{.*?^\}", r"^function chatTree\(rows\)\{.*?^\}")]
    js = "\n".join(defs) + f"""
const {{kids}} = chatTree(hangTasks({json.dumps(rows, ensure_ascii=False)}));
console.log(JSON.stringify(Object.fromEntries(Object.entries(kids).map(([k, v]) => [k, v.map(c => c.id)]))));"""
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(shutil.which("node"), "没有 node")
class SidebarTests(unittest.TestCase):
    def test_hangs_by_dispatched_from(self):
        rows = [{"id": "w-main", "title": "主对话", "last_active": "2026-11-25 10:00:00"},
                {"id": "w-a", "title": "改过的名字", "parent": "w-main", "dispatched_from": {"id": "w-main"}, "last_active": "2026-11-25 11:00:00"},
                {"id": "w-b", "title": "别的 Agent 做的", "agent": "writer", "task_of": "dev", "dispatched_from": {"id": "w-main"},
                 "last_active": "2026-11-25 12:00:00"},
                {"id": "w-c", "title": "普通对话", "last_active": "2026-11-25 09:00:00"}]
        self.assertEqual(sorted(tree(rows)["w-main"]), ["w-a", "w-b"])
        self.assertNotIn("w-c", sum(tree(rows).values(), []))

    def test_group_task_hangs_under_sub(self):
        rows = [{"id": "m-g-dev", "mention_of": "w-g", "title": "群", "last_active": "2026-11-25 10:00:00"},
                {"id": "w-x", "title": "群里派的", "dispatched_from": {"id": "w-g"}, "last_active": "2026-11-25 11:00:00"}]
        self.assertEqual(tree(rows), {"m-g-dev": ["w-x"]})


if __name__ == "__main__":
    unittest.main()
