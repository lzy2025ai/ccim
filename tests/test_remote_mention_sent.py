"""群聊里被 @ 的成员在别的电脑上：它发的文件、聊天记录随结果带回，记到群里（#357）；不跑真实对话。"""
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
        pf.peers = SimpleNamespace(forward=lambda *a: (200, "", json.dumps(next(replies)).encode()))
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


if __name__ == "__main__":
    unittest.main()
