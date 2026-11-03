"""群聊里被 @ 的成员发的文件、聊天记录（#357、#409）：别的电脑上的随结果带回群里；本机的群里 @ 那一轮两边都记，直接聊的只在子对话。"""
import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace

from ccim.mentions import Mentions
from ccim.platform import Logs, Platform


def _pf(folder):
    pf = object.__new__(Platform)
    pf.channel = SimpleNamespace(logs=Logs(folder), _emit=lambda *a: None)
    pf.background = set()
    return pf


class RemoteSentTests(unittest.TestCase):
    def setUp(self):
        self.dirs = [tempfile.TemporaryDirectory() for _ in range(2)]

    def tearDown(self):
        for d in self.dirs:
            d.cleanup()

    def test_peer_result_returns_sent_items_of_this_turn(self):
        pf = _pf(self.dirs[0].name)
        sub = "m-w-g-cai"
        pf.channel.logs.append(sub, {"type": "file", "path": "/old.png", "name": "old.png", "size": 1})   # 上一轮的
        start = time.time()
        pf.channel.logs.append(sub, {"type": "chatlog", "title": "群聊", "items": [{"sender": "甲", "text": "hi", "image": "/a.png"}], "text": "甲：hi"})
        pf.channel.logs.append(sub, {"type": "file", "path": "/b.pdf", "name": "b.pdf", "size": 3})
        pf.peer_results, pf.peer_runs = {"t": {"done": True, "text": "好了"}}, {"t": (sub, start)}
        r = pf.peer_result("t")
        self.assertEqual([x["type"] for x in r["sent"]], ["chatlog", "file"])
        self.assertEqual(r["sent"][0]["items"][0]["image"], "/a.png")
        self.assertEqual(r["apps"], [])

    def test_remote_member_sent_items_land_in_group(self):
        pf = _pf(self.dirs[1].name)
        m = pf.mentions = Mentions(pf)
        pf.chats = lambda: {"w-g": {}}
        cid, rid, sub = "w-g", "1:cai", "m-w-g-cai"
        sent = [{"type": "chatlog", "title": "群聊", "items": [{"sender": "甲", "text": "hi"}], "text": "甲：hi", "at": 5},
                {"type": "file", "path": "/b.pdf", "name": "b.pdf", "size": 3, "at": 6}]
        replies = iter([{"token": "t"}, {"done": False, "sent": sent[:1]}, {"done": True, "text": "发了", "sent": sent}])
        pf.peers = SimpleNamespace(me={"id": "mac-a"}, forward=lambda *a: (200, "", json.dumps(next(replies)).encode()))
        m.running.add(rid)
        sleep = asyncio.sleep
        asyncio.sleep = lambda *_: sleep(0)
        try:
            asyncio.run(m._run_remote(cid, rid, sub, "p", "mac-b", "cai", "群", "看下"))
        finally:
            asyncio.sleep = sleep
        rows = pf.channel.logs.read(cid)
        got = [r for r in rows if r.get("type") in ("file", "chatlog")]
        self.assertEqual([r["type"] for r in got], ["chatlog", "file"])          # 两次都带回来的只记一次
        self.assertTrue(all(r["mid"] == "mac-b" and r["sub"] == sub for r in got))
        self.assertEqual(rows[-1]["state"], "done")

    def test_local_member_posts_follow_the_turn(self):
        from ccim.platform import WebChannel
        pf = _pf(self.dirs[0].name)
        ch = object.__new__(WebChannel)
        ch.logs, ch.platform, ch._emit = pf.channel.logs, pf, lambda *a: None
        cid, sub = "w-g", "m-w-g-cai"
        pf.chats = lambda: {cid: {}, sub: {"mention_of": cid}}
        turn = SimpleNamespace(current=None)
        pf.mentions = SimpleNamespace(_chat=lambda c: turn)
        group_task = SimpleNamespace(group=cid)
        turn.current = (group_task, None)                     # 群里 @ 它的那一轮
        asyncio.run(ch.send_app(sub, {"id": "a1", "name": "应用"}))
        turn.current = (SimpleNamespace(), None)              # 用户在子对话里直接说的那一轮
        asyncio.run(ch.send_app(sub, {"id": "a2", "name": "应用"}))
        self.assertEqual([r["app"] for r in ch.logs.read(sub)], ["a1", "a2"])
        self.assertEqual([(r["app"], r["sub"]) for r in ch.logs.read(cid)], [("a1", sub)])


if __name__ == "__main__":
    unittest.main()
