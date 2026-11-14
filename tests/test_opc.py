"""一人公司（#470）：建公司、招员工（固定窗口、档案）、上下级、规章进说明、大群成员跟着走、请走、老公司收掉。不跑真实对话。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, board, registry, share


class OpcTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18984}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)

    def test_flow(self):
        from ccim import opc, platform
        pf = self.pf
        v = opc.view(pf)                     # 还没加过人：不用先建公司
        self.assertEqual((v["members"], v["group"], v["name"]), ([], None, ""))
        self.assertTrue(v["me"])
        gid = pf.call(opc.save, pf, {"name": "琼光科技", "rules": "每天收工前在大群说进展"})["group"]
        g = pf.chats()[gid]
        self.assertEqual((g["company"], g["title"], g.get("agent")), ("opc", "琼光科技 大群", None))

        pm = pf.call(opc.hire, pf, {"name": "小林", "title": "产品经理", "role": "管需求"})["id"]
        dev = pf.call(opc.hire, pf, {"name": "阿木", "title": "工程师", "boss": pm})["id"]
        a = agents.get(pm)
        self.assertTrue(a["staff"])
        self.assertFalse(a.get("dir"))
        # 固定窗口：一人一个，再开还是它
        v = {m["id"]: m for m in opc.view(pf)["members"]}
        cid = v[pm]["chat"]
        self.assertEqual(pf.chats()[cid]["staff"], pm)
        self.assertEqual(pf.chats()[cid]["agent"], pm)
        self.assertEqual(pf.call(opc.window, pf, pm)["chat"], cid)
        self.assertEqual(pf.chat_view(cid)["staff"], pm)
        self.assertIn(cid, [c["id"] for c in pf.chat_head()[0]])   # 没在干活也带在整体状态里：列表、架构图要它的最新一句
        # 档案
        d = agents.workspace(pm)
        self.assertIn("@记忆.md", open(os.path.join(d, "CLAUDE.md"), encoding="utf-8").read())
        with open(os.path.join(d, "记忆.md"), "w", encoding="utf-8") as f:
            f.write("- 11-12 老板要求周报写三句话\n")
        self.assertIn("周报", pf.call(opc.memory, pf, pm)["text"])
        # 大群成员跟着公司走
        self.assertEqual(pf.chats()[gid]["members"], [pm, dev])
        # 说明：岗位、上下级、规章
        n = opc.note_for(pf, dev)
        for t in ("琼光科技", "岗位：工程师", "直属上级：小林（产品经理）", "每天收工前在大群说进展", "记忆.md"):
            self.assertIn(t, n)
        self.assertIn("直属下级：阿木（工程师）", opc.note_for(pf, pm))
        self.assertIn("直属上级：老板", opc.note_for(pf, pm))
        self.assertEqual(opc.note_for(pf, "nobody"), "")
        # 上下级不能绕成圈
        with self.assertRaises(opc.Problem):
            pf.call(opc.edit, pf, pm, {"boss": dev})
        with self.assertRaises(opc.Problem):
            pf.call(opc.edit, pf, pm, {"boss": pm})
        # 改名、改岗位：窗口标题跟着
        pf.call(opc.edit, pf, dev, {"name": "木木", "title": "后端"})
        self.assertEqual(agents.get(dev)["name"], "木木")
        self.assertEqual(pf.chats()[v[dev]["chat"]]["title"], "木木")
        self.assertIn("直属下级：木木（后端）", opc.note_for(pf, pm))
        # 整体状态里带着；访客看不到
        st = platform.state(pf, None)
        self.assertEqual(st["opc"]["name"], "琼光科技")
        self.assertEqual(share.filter_state(pf, {"agents": [], "chats": [], "opc": st["opc"]}, "g1")["opc"], None)
        # 窗口标成已完成：再开是新的
        chats = pf.chats()
        chats[cid]["archived"] = True
        pf.store.write(chats)
        self.assertIsNone({m["id"]: m for m in opc.view(pf)["members"]}[pm]["chat"])
        new = pf.call(opc.window, pf, pm)["chat"]
        self.assertNotEqual(new, cid)
        # 请走上级：下级改向上级的上级汇报，Agent 删掉，窗口标完成，档案还在
        pf.call(opc.fire, pf, pm)
        self.assertIsNone(agents.get(pm))
        self.assertTrue(pf.chats()[new]["archived"])
        self.assertTrue(os.path.exists(os.path.join(d, "记忆.md")))
        v = opc.view(pf)["members"]
        self.assertEqual([(m["id"], m["boss"]) for m in v], [(dev, "")])
        self.assertEqual(pf.chats()[gid]["members"], [dev])
        # 改规章
        pf.call(opc.save, pf, {"rules": "周五不上线"})
        self.assertIn("周五不上线", opc.note_for(pf, dev))
        self.assertEqual(opc.view(pf)["name"], "琼光科技")

    def test_hire_without_company(self):
        from ccim import opc, people
        pf = self.pf
        mid = pf.call(opc.hire, pf, {"name": "小林"})["id"]      # 第一次加人顺手把公司、大群建上
        v = opc.view(pf)
        self.assertEqual(v["name"], f"{people.owner_name()}的公司"[:20])
        self.assertTrue(v["group"])
        self.assertEqual([m["id"] for m in v["members"]], [mid])

    def test_human_member(self):
        from ccim import opc, people
        pf = self.pf
        gid = pf.call(opc.save, pf, {"name": "琼光"})["group"]
        registry.write_state(pf.home, guests={"g1": {"name": "老王"}})
        self.assertEqual(pf.call(opc.candidates, pf)["people"], [{"id": "g1", "name": "老王"}])
        mid = pf.call(opc.hire, pf, {"account": "g1", "title": "设计师"})["id"]
        self.assertEqual(mid, "h:g1")
        self.assertEqual([p["id"] for p in people.people(pf, gid)], ["g1"])
        self.assertEqual(pf.call(opc.candidates, pf)["people"], [])
        dev = pf.call(opc.hire, pf, {"name": "阿木", "boss": mid})["id"]
        self.assertIn("真人", opc.note_for(pf, dev))
        with self.assertRaises(opc.Problem):
            pf.call(opc.window, pf, mid)
        pf.call(opc.fire, pf, mid)
        self.assertEqual(people.people(pf, gid), [])
        self.assertEqual(opc.view(pf)["members"][0]["boss"], "")

    def test_board_only_unread(self):
        """员工的窗口、大群一直开着：看过了就不算等你处理。"""
        s = {"answer_at": 100}
        self.assertTrue(board.answered({}, s))
        self.assertTrue(board.answered({"staff": "a"}, s))
        self.assertFalse(board.answered({"staff": "a", "seen_at": 200}, s))
        self.assertFalse(board.answered({"company": "opc", "seen_at": 200}, s))

    def test_old_companies_dropped(self):
        from ccim import opc
        pf = self.pf
        gid = pf.new_chat(title="接单项目 大群")
        chats = pf.chats()
        chats[gid]["company"] = "co-1"
        pf.store.write(chats)
        registry.write_state(pf.home, companies={"co-1": {"name": "接单项目", "group": gid, "members": []}})
        pf.call(opc.migrate, pf)
        self.assertFalse(registry.read_state(pf.home).get("companies"))
        g = pf.chats()[gid]
        self.assertTrue(g["archived"])
        self.assertNotIn("company", g)


if __name__ == "__main__":
    unittest.main()
