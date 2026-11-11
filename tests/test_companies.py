"""一人公司（#454）：建公司、加入 / 换公司、大群成员跟着走、公司说明、大群不 @ 只记下、删公司。不跑真实对话。"""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, registry, share


class CompanyTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18983}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        self.a = agents.create(name="花花工程师", role="写小程序")["id"]
        self.b = agents.create(name="商单管理员")["id"]

    def test_company_flow(self):
        from ccim import companies
        pf = self.pf
        r = pf.call(companies.save, pf, None, {"name": "接单项目", "note": "代码在 SD 目录"})
        coid, gid = r["id"], r["group"]
        g = pf.chats()[gid]
        self.assertEqual(g["company"], coid)
        self.assertNotIn("agent", g)
        self.assertEqual(g["title"], "接单项目 大群")
        self.assertEqual(g["note"], "代码在 SD 目录")
        pf.call(companies.join, pf, self.a, coid)
        pf.call(companies.join, pf, self.b, coid)
        self.assertEqual(pf.chats()[gid]["members"], [self.a, self.b])
        self.assertIn("代码在 SD 目录", companies.note_for(pf, self.a))
        # 换公司：从原来的公司和大群里拿掉
        r2 = pf.call(companies.save, pf, None, {"name": "个人助理"})
        pf.call(companies.join, pf, self.b, r2["id"])
        self.assertEqual(pf.chats()[gid]["members"], [self.a])
        self.assertEqual(pf.chats()[r2["group"]]["members"], [self.b])
        self.assertEqual(companies.of_agent(pf, self.b)[0], r2["id"])
        # 改名：大群标题跟着
        pf.call(companies.save, pf, coid, {"name": "外包项目"})
        self.assertEqual(pf.chats()[gid]["title"], "外包项目 大群")
        # 移出公司
        pf.call(companies.join, pf, self.a, None)
        self.assertEqual(pf.chats()[gid]["members"], [])
        self.assertEqual(companies.note_for(pf, self.a), "")
        # 大群里不 @ 人：只记下，不交给助手
        asyncio.run_coroutine_threadsafe(pf.send(gid, "大家好"), pf.loop).result(5)
        rows = [x for x in pf.channel.logs.read(gid) if x.get("type") == "user"]
        self.assertTrue(rows and rows[-1].get("quiet"))
        self.assertFalse(pf.chats()[gid].get("busy"))
        # 删公司：Agent 还在，大群标为已完成
        pf.call(companies.delete, pf, r2["id"])
        self.assertIsNotNone(agents.get(self.b))
        self.assertTrue(pf.chats()[r2["group"]].get("archived"))
        self.assertEqual([c["id"] for c in companies.view(pf)], [coid])

    def test_name_required(self):
        from ccim import companies
        with self.assertRaises(companies.Problem):
            self.pf.call(companies.save, self.pf, None, {"name": "  "})


if __name__ == "__main__":
    unittest.main()
