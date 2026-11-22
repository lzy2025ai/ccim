"""总台（#543）：派活、接着做、做完汇报、改派、反问的候选、右边一栏的分组；不跑真实对话。"""
import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ccim import desk, search
from ccim.platform import Logs, Platform


class DeskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        self.meta = {"w-desk": {"helper": "desk", "temp": {"base": "claude"}, "title": "总台"},
                     "w-old": {"agent": "dev", "title": "修登录", "last_active": "2026-11-22 10:00:00"},
                     "w-g": {"agent": "dev", "members": ["writer"], "title": "群"},
                     "m-w-g-writer": {"agent": "writer", "mention_of": "w-g"}}
        self.agents = {"dev": {"name": "开发"}, "writer": {"name": "写作"}, "boss": {"name": "员工", "staff": True}}
        self.pf = pf = object.__new__(Platform)
        pf.channel = SimpleNamespace(logs=self.logs, progress_of={}, _emit=lambda cid, rec: None)
        pf.chats = lambda: self.meta
        pf.meta = lambda cid: self.meta.get(cid) or {}
        pf.store = SimpleNamespace(write=lambda chats: None)
        pf.background, pf.works = set(), set()
        self.tasks, self.delivered, self.stopped = [], [], []
        pf._bridge = lambda cid: SimpleNamespace(chat=lambda c, t: SimpleNamespace(submit=lambda task: self.tasks.append((c, task))))
        pf.deliver = lambda cid, text: self.delivered.append((cid, text)) or True

        async def stop(cid):
            self.stopped.append(cid)
        pf.stop_chat = stop
        n = iter(range(100))

        def new_chat(agent=None, title=None, **kw):
            cid = f"w-new{next(n)}"
            self.meta[cid] = {"agent": agent, "title": title}
            return cid
        pf.new_chat = new_chat
        pf.chat_view = lambda cid: {"title": self.meta[cid].get("title"), "busy": bool(self.meta[cid].get("busy")),
                                    "attention": self.meta[cid].get("att"), "doing": None, "brief": None}
        self.p = [mock.patch("ccim.agents.all_agents", lambda: self.agents),
                  mock.patch("ccim.agents.get", lambda aid: dict(self.agents[aid], id=aid) if aid in self.agents else None),
                  mock.patch("ccim.share.owner_map", lambda pf: {})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.dir.cleanup()

    def cards(self):
        out = {}
        for r in self.logs.read("w-desk"):
            if r.get("type") == "work":
                out[r["id"]] = r
        return out

    async def test_new_chat_card_and_report(self):
        card = desk.dispatch(self.pf, "w-desk", "去查一下多门店报错", agent="开发", title="多门店报错")
        self.assertEqual((card["desk"], card["agent"], card["title"]), ("new", "dev", "多门店报错"))
        cid, task = self.tasks[-1]
        self.assertEqual(cid, card["chat"])
        self.assertIn("去查一下多门店报错", task)
        first = [r for r in self.logs.read(cid) if r.get("type") == "user"][0]
        self.assertTrue(first["desk"] and first["dispatch"])
        self.assertIn(card["id"], self.pf.works)
        task.future.set_result(("ok", "查完了，是限流"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.cards()[card["id"]]["state"], "done")
        self.assertEqual(self.delivered[-1][0], "w-desk")
        self.assertIn("查完了，是限流", self.delivered[-1][1])
        self.assertNotIn(card["id"], self.pf.works)

    async def test_continue_existing_chat(self):
        card = desk.dispatch(self.pf, "w-desk", "再改一版", chat="w-old")
        self.assertEqual((card["desk"], card["chat"], card["title"]), ("continue", "w-old", "修登录"))
        rec = [r for r in self.logs.read("w-old") if r.get("type") == "user"][-1]
        self.assertTrue(rec["relay"] and rec["desk"])
        # 群里被 @ 的子对话：算到外层群聊
        card = desk.dispatch(self.pf, "w-desk", "群里接着做", chat="m-w-g-writer")
        self.assertEqual(card["chat"], "w-g")

    def test_rejects(self):
        with self.assertRaises(desk.Problem):
            desk.dispatch(self.pf, "w-desk", "x", agent="员工")          # 一人公司的员工不派
        with self.assertRaises(desk.Problem):
            desk.dispatch(self.pf, "w-desk", "x", chat="w-desk")         # 总台自己
        with self.assertRaises(desk.Problem):
            desk.dispatch(self.pf, "w-desk", "x", agent="开发", chat="w-old")
        with self.assertRaises(desk.Problem):
            desk.dispatch(self.pf, "w-desk", "  ", agent="开发")

    async def test_reassign_stops_old_and_skips_its_report(self):
        card = desk.dispatch(self.pf, "w-desk", "写个对联", agent="开发")
        _, old = self.tasks[-1]
        new = await desk.reassign(self.pf, "w-desk", card["id"], agent="writer")
        self.assertEqual(self.stopped, [card["chat"]])
        self.assertEqual((new["agent"], new["desk"], new["text"]), ("writer", "new", "写个对联"))
        self.assertEqual(self.cards()[card["id"]]["state"], "moved")
        old.future.set_result(("stopped", ""))
        await asyncio.sleep(0.05)
        self.assertEqual(self.cards()[card["id"]]["state"], "moved")   # 停下的那一轮不汇报、不改卡片
        self.assertEqual(self.delivered, [])
        with self.assertRaises(desk.Problem):
            await desk.reassign(self.pf, "w-desk", card["id"], agent="writer")
        self.meta[card["chat"]]["att"] = "done"                       # 被停下的那边有没看的回答，也不算等你
        with mock.patch.object(desk, "find", lambda pf, acct=None: "w-desk"):
            p = desk.panel(self.pf)
        self.assertEqual([r["chat"] for r in p["working"]], [new["chat"]])
        self.assertEqual(p["waiting"], [])
        self.assertEqual(new["moved_from"], "dev")

    def test_choices(self):
        rec = desk.choices(self.pf, "w-desk", "", ["w-old", "w-g", "w-old", "没有这个"])
        self.assertEqual([x["chat"] for x in rec["items"]], ["w-old", "w-g"])
        self.assertEqual(rec["q"], "你说的是哪一件？")
        with self.assertRaises(desk.Problem):
            desk.choices(self.pf, "w-desk", "", ["没有"])

    async def test_panel_groups(self):
        a = desk.dispatch(self.pf, "w-desk", "一", agent="开发")
        b = desk.dispatch(self.pf, "w-desk", "二", agent="writer")
        c = desk.dispatch(self.pf, "w-desk", "三", chat="w-old")
        self.meta[a["chat"]]["busy"] = True
        for cid, t in self.tasks[1:]:
            t.future.set_result(("ok", "好了"))
        await asyncio.sleep(0.05)
        self.meta[b["chat"]]["att"] = "done"
        with mock.patch.object(desk, "find", lambda pf, acct=None: "w-desk"):
            p = desk.panel(self.pf)
        self.assertEqual([r["chat"] for r in p["working"]], [a["chat"]])
        self.assertEqual([(r["chat"], r["line"]) for r in p["waiting"]], [(b["chat"], "做完了，等你验收")])
        self.assertEqual([r["chat"] for r in p["done"]], ["w-old"])
        self.assertTrue(c)

    def test_search_everyone(self):
        self.pf.mentions = SimpleNamespace(_name=lambda m, rec: self.agents[m["agent"]]["name"],
                                           _agent_name=lambda aid: self.agents[aid]["name"])
        src = search.sources(self.pf, search.ALL, None)
        self.assertEqual(set(src), {"w-old", "w-g"})          # 不含总台自己、群里的子对话
        self.assertEqual(src["w-g"][1], "群聊")


if __name__ == "__main__":
    unittest.main()
