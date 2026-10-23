"""共享电脑（#154）：访客经总控转来的请求只能动自己的东西，主人看不到访客的；不读真实登记表、不碰真实主目录。"""
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

    def req(self, method, path, body=None, guest="a1"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if guest:
            h.update({"X-From-Peer": "1", "X-Account": guest, "X-Account-Name": "%E5%AE%B6%E4%BA%BA"})
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
        self.assertEqual(a["dir"], os.path.join(self.home, "ccim共享", "家人", "写作"))
        st, r = self.req("POST", "/api/agent/save", {"id": aid, "fields": {"name": "写作", "dir": "/tmp"}})
        self.assertEqual(st, 403)
        st, r = self.req("POST", "/api/agent/save", {"id": aid, "fields": {"name": "写作", "account": None}})
        self.assertEqual(agents.get(aid)["account"], "a1")
        st, r = self.req("GET", "/api/dirs?path=/")          # 文件夹只能在自己的里面选
        self.assertEqual(r["path"], os.path.join(self.home, "ccim共享", "家人"))
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
                           ("/api/channel/stop", {"proc": "x"}), ("/api/skills/delete", {"name": "x"}),
                           ("/api/helper", {"kind": "skills"}), ("/api/peer/task", {}), ("/api/agent/connect", {"id": "x"}),
                           ("/api/project/save", {}), ("/api/chat/continue", {"id": "x:1"})):
            self.assertEqual(self.req("POST", path, body)[0], 403, path)
        for path in ("/api/fs/ls", "/api/logs?proc=x", "/api/recent", "/api/skills/local", "/api/project?id=x"):
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
        self.assertEqual(self.pf.schedules.all()[s["id"]]["deliver"], [])
        _, o = self.req("GET", "/api/state?local=1", guest=None)
        self.assertEqual(o["schedules"], [])
        _, g = self.req("GET", "/api/schedule/list")
        self.assertEqual([x["id"] for x in g["schedules"]], [s["id"]])

    def test_header_ignored_without_peer(self):
        """本机网页（没经过总控）带 X-Account 不算访客。"""
        self.assertIsNone(share.account_of({"X-Account": "a1"}))


if __name__ == "__main__":
    unittest.main()
