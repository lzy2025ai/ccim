"""群聊里的真人（#419）：拉人、邀请和同意、谁能做什么、多人时只回 @、有访客时会话按访客的权限跑。不启动真实会话。"""
import time
import unittest

from ccim import people, share
from ccim.peers import OWNER

from test_share import ShareTests


class PeopleTests(unittest.TestCase):
    setUp = ShareTests.setUp                  # 同一套临时环境，不重复跑共享电脑的用例
    req = ShareTests.req

    def guest_chat(self):
        st, r = self.req("POST", "/api/chat/new", {"temp": {}})
        self.assertEqual(st, 200, r)
        return r["id"]

    def owner_chat(self):
        st, r = self.req("POST", "/api/chat/new", {"agent": self.owner_agent}, guest=None)
        self.assertEqual(st, 200, r)
        return r["id"]

    def wait_log(self, cid, n):
        for _ in range(50):
            log = [r for r in self.pf.channel.logs.read(cid) if r.get("type") == "user"]
            if len(log) >= n:
                return log
            time.sleep(0.05)
        self.fail("消息没记下")

    def test_guest_invites_owner(self):
        cid = self.guest_chat()
        st, r = self.req("GET", f"/api/chat/people?id={cid}")
        self.assertEqual(r["role"], "creator")
        self.assertIn(OWNER, [c["id"] for c in r["candidates"]])
        st, r = self.req("POST", "/api/chat/people", {"id": cid, "add": [OWNER]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["people"][0]["state"], "invited")      # 访客拉的要对方同意

        st, s = self.req("GET", "/api/state?local=1", guest=None)            # 主人的列表里有这个邀请
        row = next(c for c in s["chats"] if c["id"] == cid)
        self.assertEqual(row["people_role"], "invited")
        self.assertTrue(row["invite"])
        st, d = self.req("GET", f"/api/chat?id={cid}", guest=None)
        self.assertEqual(d["log"], [])
        self.assertTrue(d["invite"])
        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "hi"}, guest=None)
        self.assertEqual(st, 403)

        st, r = self.req("POST", "/api/chat/join", {"id": cid, "accept": True}, guest=None)
        self.assertEqual(st, 200, r)
        self.assertEqual(people.state_of(self.pf, cid, OWNER), "in")
        self.assertTrue(people.quiet(self.pf, cid))

        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "我看看"}, guest=None)
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/chat/send", {"id": cid, "text": "好", "at": [OWNER]})
        log = self.wait_log(cid, 2)
        self.assertTrue(all(x.get("quiet") for x in log))             # 两个人都没 @ Agent：只记下
        self.assertEqual(log[0]["person"]["id"], OWNER)
        self.assertEqual(log[1]["person"]["name"], "家人")
        self.assertFalse(self.pf.chats()[cid].get("busy"))

        st, s = self.req("GET", "/api/state?local=1", guest=None)            # 被 @ 了：在主人那边算等你处理
        row = next(c for c in s["chats"] if c["id"] == cid)
        self.assertTrue(row.get("at_me"))

        for path, body in (("/api/chat/delete", {"id": cid}), ("/api/chat/rewind", {"id": cid, "index": 0}),
                           ("/api/chat/set", {"id": cid, "model": "x"}), ("/api/chat/fork", {"id": cid, "index": 0})):
            st, r = self.req("POST", path, body, guest=None)
            self.assertEqual(st, 403, path)

        bl = people.backlog(self.pf, cid)                              # 下次 @ Agent 时把这些带上
        self.assertIn("我看看", bl)
        self.assertEqual(people.backlog(self.pf, cid), "")

        st, r = self.req("POST", "/api/chat/people", {"id": cid, "remove": [OWNER]}, guest=None)   # 自己退出
        self.assertEqual(st, 200, r)
        st, s = self.req("GET", "/api/state?local=1", guest=None)
        self.assertNotIn(cid, [c["id"] for c in s["chats"]])

    def test_owner_pulls_guest(self):
        self.req("GET", "/api/state?local=1")                                  # 访客来过这台电脑，主人才拉得到
        cid = self.owner_chat()
        st, r = self.req("POST", "/api/chat/people", {"id": cid, "add": ["a1"]}, guest=None)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["people"][0]["state"], "in")                # 主人拉的直接进
        st, s = self.req("GET", "/api/state?local=1")
        self.assertIn(cid, [c["id"] for c in s["chats"]])
        st, d = self.req("GET", f"/api/chat?id={cid}")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["people_role"], "in")
        self.assertEqual(d["people_view"]["agents"][0]["id"], self.owner_agent)

        b = self.pf._bridge(cid)                                       # 有访客：会话圈起来，不能借它看主人的东西
        self.assertTrue(b.entry["guest"].startswith(people.FENCE))
        self.assertTrue(b.entry["lent"])

        other = self.owner_chat()                                      # 别的对话访客照旧看不到
        st, r = self.req("GET", f"/api/chat?id={other}")
        self.assertEqual(st, 403)
        st, r = self.req("POST", "/api/chat/people", {"id": cid, "remove": [OWNER]})
        self.assertEqual(st, 400)                                      # 只有建群的人能移别人
        self.assertIn("建群", r["error"])

        ev = share.tag(self.pf, {"type": "progress", "chat": cid})
        self.assertEqual(ev.get("_p"), ["a1"])

        st, r = self.req("POST", "/api/chat/people", {"id": cid, "remove": ["a1"]}, guest=None)
        self.assertEqual(st, 200, r)
        self.assertIsNone(people.fence_of(self.pf, cid))

    def test_hub_visible(self):
        from ccim.hubserver import Hub
        hub = Hub.__new__(Hub)
        hub.allowed = lambda aid: {"m1"}

        class P:
            def guest_of(self, mid, aid):
                return aid != OWNER
        hub.peers = P()
        self.assertTrue(hub.visible("a1", {"_m": "m1", "chat": "c", "_p": ["a1"]}))
        self.assertTrue(hub.visible(OWNER, {"_m": "m1", "chat": "c", "_a": "a1", "_p": [OWNER]}))
        self.assertFalse(hub.visible(OWNER, {"_m": "m1", "chat": "c", "_a": "a1"}))
        self.assertFalse(hub.visible("a2", {"_m": "m1", "chat": "c", "_a": "a1", "_p": [OWNER]}))


del ShareTests                                # 不让 unittest 在这个文件里再收集一遍

if __name__ == "__main__":
    unittest.main()
