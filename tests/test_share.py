"""共享电脑（#154）：访客经总控转来的请求只能动自己的、和主人共享给它的，主人看不到访客的；不读真实登记表、不碰真实主目录。"""
import asyncio
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccim import agents, registry, share


class ShareTests(unittest.TestCase):
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
        self.home = home
        from ccim import platform
        from ccim.peers import Peers
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18977}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        pf.external_chats = lambda *a, **k: []
        self.server = pf.serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.owner_agent = agents.create(name="主人的助手")["id"]

    def req(self, method, path, body=None, guest="a1", codex=False, agents_=(), own=True, folder=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if guest:
            from urllib.parse import quote
            opts = {"agents": list(agents_), "own": own, "codex": codex, "dir": folder}
            h.update({"X-From-Peer": "1", "X-Account": guest, "X-Account-Name": "%E5%AE%B6%E4%BA%BA",
                      "X-Account-Share": quote(json.dumps(opts))})
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def guest_agent(self, name="写作"):
        st, r = self.req("POST", "/api/agent/save", {"id": None, "fields": {"name": name, "chrome": True}})
        self.assertEqual(st, 200, r)
        return r["id"]

    def test_guest_agent_lives_in_its_folder(self):
        aid = self.guest_agent()
        a = agents.get(aid)
        self.assertEqual(a["account"], "a1")
        self.assertFalse(a.get("chrome"))
        self.assertEqual(a["dir"], os.path.join(self.home, ".ccim", "shared", "家人", "写作"))
        st, r = self.req("POST", "/api/agent/save", {"id": aid, "fields": {"name": "写作", "dir": "/tmp"}})
        self.assertEqual(st, 403)
        st, r = self.req("POST", "/api/agent/save", {"id": aid, "fields": {"name": "写作", "account": None}})
        self.assertEqual(agents.get(aid)["account"], "a1")
        st, r = self.req("GET", "/api/dirs?path=/")          # 文件夹只能在自己的里面选
        self.assertEqual(r["path"], os.path.join(self.home, ".ccim", "shared", "家人"))
        self.assertIsNone(r["parent"])

    def test_state_split(self):
        aid = self.guest_agent()
        _, g = self.req("GET", "/api/state?local=1")
        self.assertEqual([a["id"] for a in g["agents"]], [aid])
        _, o = self.req("GET", "/api/state?local=1", guest=None)
        self.assertEqual([a["id"] for a in o["agents"]], [self.owner_agent])
        _, other = self.req("GET", "/api/state?local=1", guest="a2")
        self.assertEqual(other["agents"], [])
        self.assertEqual(self.req("GET", f"/api/agent?id={self.owner_agent}")[0], 403)
        self.assertEqual(self.req("GET", f"/api/agent?id={aid}", guest="a2")[0], 403)

    def test_chats_belong_to_agent_owner(self):
        aid = self.guest_agent()
        self.assertEqual(self.req("POST", "/api/chat/new", {"agent": self.owner_agent})[0], 403)
        st, r = self.req("POST", "/api/chat/new", {"agent": aid})
        self.assertEqual(st, 200)
        cid = r["id"]
        st, t = self.req("POST", "/api/chat/new", {"temp": {"base": "claude"}})
        self.assertEqual(self.pf.chats()[t["id"]]["account"], "a1")
        self.assertEqual(self.req("POST", "/api/chat/new", {"temp": {"base": "claude", "dir": self.home}})[0], 403)
        _, o = self.req("GET", "/api/state?local=1", guest=None)
        self.assertNotIn(cid, [c["id"] for c in o["chats"]])
        self.assertNotIn(t["id"], [c["id"] for c in o["chats"]])
        _, g = self.req("GET", "/api/state?local=1")
        self.assertEqual({c["id"] for c in g["chats"]}, {cid, t["id"]})
        _, owner_chat = self.req("POST", "/api/chat/new", {"agent": self.owner_agent}, guest=None)
        self.assertEqual(self.req("GET", f"/api/chat?id={owner_chat['id']}")[0], 403)
        self.assertEqual(self.req("POST", "/api/chat/send", {"id": owner_chat["id"], "text": "hi"})[0], 403)
        self.assertEqual(self.req("POST", "/api/chat/delete", {"id": owner_chat["id"]})[0], 403)
        self.assertEqual(self.req("GET", f"/api/file?chat={owner_chat['id']}&path=/etc/hosts")[0], 403)
        q = self.pf.channel.events.subscribe()       # 推送标上是谁的
        self.pf.channel.events.publish({"type": "idle", "chat": cid})
        self.pf.channel.events.publish({"type": "idle", "chat": owner_chat["id"]})
        self.assertEqual(q.get(timeout=2).get("_a"), "a1")
        self.assertNotIn("_a", q.get(timeout=2))

    def test_owner_only_operations(self):
        for path, body in (("/api/fs/trash", {"paths": [self.home]}), ("/api/global", {"model": "x"}),
                           ("/api/chat/secret", {"id": "x", "run": "dialog"}), ("/api/upgrade", {}),
                           ("/api/channel/stop", {"proc": "x"}), ("/api/peer/task", {}),
                           ("/api/agent/connect", {"id": self.owner_agent, "channel": "weixin"}),
                           ("/api/project/save", {}), ("/api/chat/continue", {"id": "x:1"})):
            self.assertEqual(self.req("POST", path, body)[0], 403, path)
        for path in ("/api/fs/ls", "/api/logs?proc=x", "/api/recent", "/api/skills/local", "/api/project?id=x", "/api/job?id=x"):
            self.assertEqual(self.req("GET", path)[0], 403, path)

    def test_kb_and_schedules(self):
        aid = self.guest_agent()
        st, lib = self.req("POST", "/api/kb/lib/save", {"name": "家里的事"})
        self.assertEqual(st, 200)
        _, o = self.req("GET", "/api/kb", guest=None)
        self.assertEqual(o["libs"], [])
        _, g = self.req("GET", "/api/kb")
        self.assertEqual([x["id"] for x in g["libs"]], [lib["id"]])
        _, olib = self.req("POST", "/api/kb/lib/save", {"name": "主人的库"}, guest=None)
        self.assertEqual(self.req("GET", f"/api/kb/lib?id={olib['id']}")[0], 403)
        self.assertEqual(self.req("POST", "/api/kb/entry/save", {"lib": olib["id"], "title": "x", "content": "y"})[0], 403)
        f = {"name": "早报", "prompt": "汇总", "freq": "daily", "time": "08:00", "deliver": ["feishu"]}
        self.assertEqual(self.req("POST", "/api/schedule/save", {"fields": dict(f, target={"kind": "agent", "id": self.owner_agent})})[0], 403)
        st, s = self.req("POST", "/api/schedule/save", {"fields": dict(f, target={"kind": "agent", "id": aid})})
        self.assertEqual(st, 200, s)
        self.assertEqual(self.pf.schedules.all()[s["id"]]["deliver"], ["feishu"])   # 发给它自己的 Agent 接的机器人
        _, o = self.req("GET", "/api/state?local=1", guest=None)
        self.assertEqual(o["schedules"], [])
        _, g = self.req("GET", "/api/schedule/list")
        self.assertEqual([x["id"] for x in g["schedules"]], [s["id"]])

    def test_channels_of_own_agents(self):
        """访客的 Agent 接的飞书、微信：渠道归它管，在里面的对话归它看；主人的碰不到。"""
        aid = self.guest_agent()
        mine = agents.get(aid)["dir"]
        with patch.object(agents, "linked_paths", lambda a: [("feishu", mine, mine)] if a == aid
                          else [("feishu", "/owner/proc", "/owner/proc")] if a == self.owner_agent else []):
            self.assertEqual(share.procs_of("a1"), {mine: mine})
            self.assertRaises(share.Denied, share._mine_proc, "a1", "/owner/proc")
            self.assertRaises(share.Denied, share._mine_proc, "a1", mine, "/elsewhere")
            share._mine_proc("a1", mine, mine)
            self.assertEqual(self.req("POST", "/api/channel/stop", {"proc": "/owner/proc"})[0], 403)
            self.assertEqual(self.req("GET", "/api/logs?proc=/owner/proc")[0], 403)
        rows = [{"id": "x:s1", "sid": "s1", "agent": aid}, {"id": "x:s2", "sid": "s2", "agent": self.owner_agent}]
        self.pf.external_chats = lambda *a, **k: [dict(r) for r in rows]
        self.pf._external = lambda cid: next(r for r in rows if r["id"] == cid)
        share._mine_chat(self.pf, "a1", "x:s1")
        self.assertRaises(share.Denied, share._mine_chat, self.pf, "a1", "x:s2")
        st = share.filter_state(self.pf, {"agents": [], "chats": [dict(r) for r in rows], "schedules": []}, "a1")
        self.assertEqual([c["id"] for c in st["chats"]], ["x:s1"])
        st = share.filter_state(self.pf, {"agents": [], "chats": [dict(r) for r in rows], "schedules": []}, None)
        self.assertEqual([c["id"] for c in st["chats"]], ["x:s2"])
        from ccim import session
        b = type("B", (), {"entry": {"agent_id": aid}})()
        self.assertEqual(session.guest_of(b), "a1")            # 飞书、微信里也不给管理员权限
        b.entry = {"agent_id": self.owner_agent}
        self.assertFalse(session.guest_of(b))

    def test_fence(self):
        """访客的会话：主目录里只留专属文件夹、平台 Skill、它自己的知识库；Codex 不给用。"""
        aid = self.guest_agent()
        os.makedirs(os.path.join(self.home, "Desktop"))
        os.makedirs(os.path.join(self.home, ".ccim", "shared", "别人"))
        _, mine = self.req("POST", "/api/kb/lib/save", {"name": "我的"})
        _, other = self.req("POST", "/api/kb/lib/save", {"name": "主人的"}, guest=None)
        sb, rules = share.fence("a1")
        self.assertFalse(sb["allowUnsandboxedCommands"])
        deny = " ".join(rules["permissions"]["deny"])
        for p in ("Desktop", ".ccim/shared/别人", f".ccim/kb/{other['id']}", ".ccim/agents.json"):
            self.assertIn(f"Read(/{os.path.join(self.home, p)}/**)", deny, p)
        for p in (".ccim/shared/家人", f".ccim/kb/{mine['id']}", ".ccim/skills", ".ccim", ".ccim/shared"):
            self.assertNotIn(os.path.join(self.home, p) + ")", deny, p)
        self.assertTrue(share.may_send("a1", os.path.join(agents.get(aid)["dir"], "a.txt")))
        self.assertFalse(share.may_send("a1", os.path.join(self.home, "Desktop", "a.txt")))
        self.assertEqual(self.req("POST", "/api/agent/save", {"id": None, "fields": {"name": "c", "base": "codex"}})[0], 403)
        _, t = self.req("POST", "/api/chat/new", {"temp": {"base": "codex"}})
        self.assertEqual(self.pf.chats()[t["id"]]["temp"]["base"], "claude")
        self.assertEqual(self.pf.chats()[t["id"]]["temp"]["dir"], os.path.join(self.home, ".ccim", "shared", "家人"))
        # 主人共享时勾了「也能用 Codex」
        with patch("ccim.web._codex", lambda: True):
            st, r = self.req("POST", "/api/agent/save", {"id": None, "fields": {"name": "c", "base": "codex"}}, codex=True)
        self.assertEqual(st, 200, r)
        self.assertTrue(share.codex_ok("a1"))
        _, t = self.req("POST", "/api/chat/new", {"temp": {"base": "codex"}}, codex=True)
        self.assertEqual(self.pf.chats()[t["id"]]["temp"]["base"], "codex")
        self.req("GET", "/api/state?local=1")                # 主人又关掉了：下一次来就记下
        self.assertFalse(share.codex_ok("a1"))

    def test_owner_lends_agents(self):
        """主人共享给它的 Agent：能开自己的对话，改不了、删不了、接不了飞书微信；两边的对话互相看不到。"""
        lend = dict(agents_=[self.owner_agent])
        _, g = self.req("GET", "/api/state?local=1", **lend)
        self.assertEqual([(a["id"], a.get("shared")) for a in g["agents"]], [(self.owner_agent, True)])
        _, o_chat = self.req("POST", "/api/chat/new", {"agent": self.owner_agent}, guest=None)
        st, r = self.req("POST", "/api/chat/new", {"agent": self.owner_agent}, **lend)
        self.assertEqual(st, 200)
        cid = r["id"]
        self.assertEqual(self.pf.chats()[cid]["account"], "a1")
        for path, body in (("/api/agent/save", {"id": self.owner_agent, "fields": {"name": "x"}}),
                           ("/api/agent/delete", {"id": self.owner_agent}), ("/api/agent/pin", {"id": self.owner_agent, "on": True}),
                           ("/api/agent/connect", {"id": self.owner_agent, "channel": "weixin"})):
            self.assertEqual(self.req("POST", path, body, **lend)[0], 403, path)
        _, d = self.req("GET", f"/api/agent?id={self.owner_agent}", **lend)
        self.assertEqual([c["id"] for c in d["chats"]], [cid])
        self.assertEqual(d["channels"], [])
        _, d = self.req("GET", f"/api/agent?id={self.owner_agent}", guest=None)
        self.assertEqual([c["id"] for c in d["chats"]], [o_chat["id"]])
        _, o = self.req("GET", "/api/state?local=1", guest=None)
        self.assertNotIn(cid, [c["id"] for c in o["chats"]])
        b = self.pf._bridge(cid)                              # 访客的对话单独一个 Bridge：圈住、不给看别的对话
        self.assertEqual((b.entry["guest"], b.entry["lent"]), ("a1", True))
        self.assertIsNot(b, self.pf._bridge(o_chat["id"]))
        self.assertFalse(self.pf._bridge(o_chat["id"]).entry["guest"])
        self.assertEqual(self.req("POST", "/api/chat/new", {"agent": self.owner_agent})[0], 403)   # 主人取消共享了
        self.assertEqual(self.req("GET", f"/api/chat?id={cid}")[0], 200)     # 之前的对话还是它的

    def test_owner_settings(self):
        """主人关掉「自己建 Agent」、指定共享文件夹。"""
        self.assertEqual(self.req("POST", "/api/agent/save", {"id": None, "fields": {"name": "x"}}, own=False)[0], 403)
        d = os.path.join(self.home, "家人的地方")
        self.req("GET", "/api/state?local=1", folder=d)
        self.assertEqual(share.root("a1"), d)
        self.req("GET", "/api/state?local=1", folder=self.home)   # 主目录不行：用默认的
        self.assertEqual(share.root("a1"), os.path.join(self.home, ".ccim", "shared", "家人"))

    def test_fence_keeps_lent_agent_dir(self):
        """共享的 Agent 在主人的项目里干活：那个目录放行，旁边的照样拦。"""
        proj = os.path.join(self.home, "Desktop", "proj")
        os.makedirs(proj)
        os.makedirs(os.path.join(self.home, "Desktop", "secret"))
        open(os.path.join(self.home, "Desktop", "note.txt"), "w").close()
        self.req("GET", "/api/state?local=1")
        _, rules = share.fence("a1", proj)
        deny = rules["permissions"]["deny"]
        self.assertIn(f"Read(/{self.home}/Desktop/secret/**)", deny)
        self.assertIn(f"Read(/{self.home}/Desktop/note.txt)", deny)
        self.assertNotIn(f"Read(/{self.home}/Desktop/**)", deny)
        self.assertFalse(any("Desktop/proj" in r for r in deny))
        self.assertTrue(share.may_send("a1", os.path.join(proj, "a.txt"), proj))

    def test_guest_skills(self):
        """访客装的 Skill 在它自己那一份里：主人看不到；主人的它能看、能用，改不了。"""
        from ccim import skills

        def mk(name, guest=None):
            tok = skills.SPACE.set(guest)
            try:
                skills.write_any(name, "SKILL.md", f"---\nname: {name}\ndescription: {name} 的说明\n---\n正文")
            finally:
                skills.SPACE.reset(tok)
        mk("owner-skill")
        mk("same")
        self.assertEqual(self.req("POST", "/api/skills/install", {"local": "x"})[0], 403)
        mk("my-skill", "a1")
        mk("same", "a1")
        _, g = self.req("GET", "/api/skills")
        self.assertEqual({(m["name"], bool(m.get("shared"))) for m in g["skills"]},
                         {("my-skill", False), ("same", False), ("owner-skill", True)})
        _, o = self.req("GET", "/api/skills", guest=None)
        self.assertEqual({m["name"] for m in o["skills"]}, {"owner-skill", "same"})
        _, d = self.req("GET", "/api/skills/detail?name=owner-skill")
        self.assertTrue(d.get("shared"))
        st, _ = self.req("POST", "/api/skills/file/save", {"name": "owner-skill", "path": "SKILL.md", "text": "x", "mtime": 0})
        self.assertEqual(st, 400)                             # 主人的改不了（它那一份里没有）
        self.assertEqual(self.req("POST", "/api/skills/delete", {"name": "owner-skill"})[0], 200)
        self.assertIn("owner-skill", {m["name"] for m in skills.listing()})   # 删的是它自己那份（没有这个），主人的还在
        aid = self.guest_agent()
        view = skills.plugin_for(agents.get(aid))
        links = {n: os.readlink(os.path.join(view, "skills", n)) for n in os.listdir(os.path.join(view, "skills"))}
        self.assertEqual(set(links), {"my-skill", "same", "owner-skill"})
        self.assertIn(os.path.join("guests", "a1"), links["same"])      # 同名的用它自己的
        self.assertNotIn("guests", skills.plugin_for(agents.get(self.owner_agent)) and
                         os.readlink(os.path.join(skills.plugin_for(agents.get(self.owner_agent)), "skills", "same")))
        self.req("GET", "/api/state?local=1")
        _, rules = share.fence("a1")
        deny = " ".join(rules["permissions"]["deny"])
        self.assertNotIn(os.path.join(".ccim", "skills", "lib") + "/**", deny)
        os.makedirs(os.path.join(self.home, ".ccim", "skills", "guests", "a2"), exist_ok=True)
        _, rules = share.fence("a1")
        self.assertIn(f"Read(/{os.path.join(self.home, '.ccim', 'skills', 'guests', 'a2')}/**)", rules["permissions"]["deny"])

    def test_guest_skill_helper(self):
        """Skill 助手：访客各自一个，在它的共享文件夹里，圈住；主人那个不受影响。"""
        _, g = self.req("POST", "/api/helper", {"kind": "skills"})
        _, g2 = self.req("POST", "/api/helper", {"kind": "skills"})
        _, o = self.req("POST", "/api/helper", {"kind": "skills"}, guest=None)
        self.assertEqual(g["id"], g2["id"])
        self.assertNotEqual(g["id"], o["id"])
        m = self.pf.chats()[g["id"]]
        self.assertEqual((m["account"], m["temp"]["dir"]), ("a1", os.path.join(self.home, ".ccim", "shared", "家人")))
        b = self.pf._bridge(g["id"])
        self.assertEqual(b.entry["guest"], "a1")
        self.assertIn("别人共享给他的电脑", b.entry["role"])
        self.assertFalse(self.pf._bridge(o["id"]).entry["guest"])

    def test_guest_defaults_shown(self):
        """访客看得到主人设的默认模型（只读）；在共享文件夹里新填的目录自动建好。"""
        registry.set_global(model="opus", effort="medium")
        _, g = self.req("GET", "/api/state?local=1")
        self.assertEqual((g["global"].get("model"), g["global"].get("effort")), ("opus", "medium"))
        d = os.path.join(self.home, ".ccim", "shared", "家人", "新的")
        st, r = self.req("POST", "/api/agent/save", {"id": None, "fields": {"name": "新的", "dir": d}})
        self.assertEqual(st, 200, r)
        self.assertTrue(os.path.isdir(d))

    def test_header_ignored_without_peer(self):
        """本机网页（没经过总控）带 X-Account 不算访客。"""
        self.assertIsNone(share.account_of({"X-Account": "a1"}))


if __name__ == "__main__":
    unittest.main()
