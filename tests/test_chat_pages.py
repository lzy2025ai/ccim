"""对话列表分页（#268）：整体状态只带最近 50 条和常驻的，更早的、已归档的按页取，搜索、群聊另取；不碰真实主目录。"""
import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccim import agents, registry, share


def ts(n):
    """n 分钟前，和记录里 last_active 一样的写法。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - n * 60))


class ChatPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        for p in (patch.object(registry, "HOME", os.path.join(home, ".ccim")),
                  patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
                  patch.dict(os.environ, {"HOME": home})):
            p.start()
            self.addCleanup(p.stop)
        from ccim import platform
        from ccim.peers import Peers
        self.P = platform
        h = os.path.join(home, ".ccim", "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18977}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        pf.external_chats = lambda *a, **k: []
        self.agent = agents.create(name="助手")["id"]
        # 120 个临时对话，第 i 个是 i 分钟前聊的；另外一些特别的
        chats = {f"w-{i:03d}": {"title": f"对话{i}", "last_active": ts(i), "created": ts(i)} for i in range(120)}
        chats["w-090"]["pin"] = -1                                       # 置顶的：很早也要带
        chats["w-095"]["pin"] = -2                                     # 星标合进置顶后（#347）也是置顶
        chats["w-099"]["busy"] = True                                    # 在干活
        chats["w-010"]["parent"] = "w-100"                               # 分叉出来的：原对话也带上
        chats["w-111"]["title"] = "很早的周报草稿"
        for i in (5, 115):                                               # 已归档的
            chats[f"w-{i:03d}"]["archived"] = True
        chats["w-200"] = {"title": "群聊", "agent": self.agent, "members": ["a2"], "last_active": ts(600), "created": ts(600)}
        chats["w-201"] = {"title": "和助手聊", "agent": self.agent, "last_active": ts(1), "created": ts(1)}
        chats["w-300"] = {"title": "访客的", "last_active": ts(0), "created": ts(0), "account": "a1"}
        registry.write_state(h, chats=chats)
        self.owners = patch.object(share, "owner_map", lambda pf: {cid: ("a1" if cid == "w-300" else None) for cid in chats})
        self.owners.start()
        self.addCleanup(self.owners.stop)

    def head(self):
        rows, more = self.pf.chat_head()
        return [r["id"] for r in rows], more

    def test_head_keeps_recent_and_resident(self):
        ids, more = self.head()
        recent = [f"w-{i:03d}" for i in range(52) if i != 5][:50]       # 没归档的最近 50 条
        self.assertTrue(set(recent) <= set(ids))
        for cid in ("w-090", "w-095", "w-099", "w-100"):                  # 置顶、在干活、分叉前的原对话
            self.assertIn(cid, ids)
        self.assertNotIn("w-111", ids)
        self.assertNotIn("w-201", ids)                                    # 和 Agent 的对话（没在干活）不带
        self.assertNotIn("w-300", ids)                                    # 访客的不占主人的名额
        self.assertEqual(more["archived"], 2)
        self.assertEqual(more["older"], 120 - 2 - 50 - 4)                 # 去掉归档、最近 50、常驻带上的 4 个

    def test_pages_cover_everything_once(self):
        ids, more = self.head()
        got, nxt, pages = set(ids), more["next"], 0
        while nxt:
            pg = self.pf.chat_page(None, nxt)
            self.assertLessEqual(len(pg["rows"]), self.P.CHAT_PAGE)
            got |= {r["id"] for r in pg["rows"]}
            nxt, pages = pg["next"], pages + 1
        got |= {r["id"] for r in self.pf.chat_page(None, None, True)["rows"]}
        listed = {f"w-{i:03d}" for i in range(120)}
        self.assertEqual(listed - got, set())
        self.assertEqual(pages, 2)

    def test_same_second_not_lost(self):
        chats = registry.read_state(self.pf.home)["chats"]
        for i in range(48, 54):                                           # 第 50 条前后几条同一秒
            chats[f"w-{i:03d}"]["last_active"] = chats["w-050"]["last_active"]
        registry.write_state(self.pf.home, chats=chats)
        ids, more = self.head()
        got = set(ids) | {r["id"] for r in self.pf.chat_page(None, more["next"])["rows"]}
        self.assertTrue({f"w-{i:03d}" for i in range(48, 54)} <= got)

    def test_search_groups_and_guest(self):
        self.assertEqual([r["id"] for r in self.pf.chat_page(None, q="周报 草稿")["rows"]], ["w-111"])
        self.assertEqual([r["id"] for r in self.pf.chat_page(None, groups=True)["rows"]], ["w-200"])
        rows, more = self.pf.chat_head("a1")
        self.assertEqual([r["id"] for r in rows], ["w-300"])
        self.assertEqual(more["older"], 0)

    def test_board_open_and_day_reviews(self):
        """等你处理（#390）：没标完成、回答过的都在，不管多早；在干活的、标完成的、没回答过的不在；
        定时任务的对话你没在里面说过话的不算。轮数只数你自己发的。"""
        from ccim import board
        chats = registry.read_state(self.pf.home)["chats"]
        chats["w-202"] = {"title": "10月25日 复盘", "agent": self.agent, "review": "2026-10-25", "last_active": ts(5000)}
        chats["s-x-" + self.agent] = {"title": "日报", "agent": self.agent, "schedule": "x", "last_active": ts(3)}
        chats["s-y-" + self.agent] = {"title": "周报", "agent": self.agent, "schedule": "y", "last_active": ts(3)}
        registry.write_state(self.pf.home, chats=chats)
        logs = self.pf.channel.logs
        for cid in ("w-111", "w-099", "w-115", "w-201", "s-x-" + self.agent, "s-y-" + self.agent):
            logs.append(cid, {"type": "user", "text": "定时", "sched": "x"} if cid.startswith("s-") else {"type": "user", "text": "做一下"})
            logs.append(cid, {"type": "answer", "text": "好了", "state": "done"})
        logs.append("w-111", {"type": "user", "text": "再改改"})
        logs.append("w-111", {"type": "user", "text": "转来的", "from_chat": "w-001"})
        logs.append("w-111", {"type": "answer", "text": "改好了", "state": "done"})
        logs.append("s-y-" + self.agent, {"type": "user", "text": "这周的再细一点"})
        logs.append("w-050", {"type": "user", "text": "还没回答"})
        rows = {r["id"]: r for r in board.open_rows(self.pf, self.pf.chats())}
        self.assertEqual(rows["w-111"]["rounds"], 2)                     # 很早的也在；转来的不算一轮
        self.assertIn("w-201", rows)                                      # 和 Agent 的对话也在
        self.assertIn("s-y-" + self.agent, rows)                          # 定时任务的对话，你在里面说过话
        for cid in ("w-099", "w-115", "w-050", "s-x-" + self.agent):     # 在干活、标完成、没回答过、没说过话的定时任务
            self.assertNotIn(cid, rows)
        self.assertTrue(rows["w-111"]["answered"])
        self.assertFalse(self.pf.chat_view("s-x-" + self.agent)["answered"])
        self.assertEqual([r["id"] for r in board.day(self.pf, "2026-10-25")["reviews"]], ["w-202"])

    def test_http(self):
        server = self.pf.serve()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("GET", path, headers={"X-CCIM": "1", "Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            out = r.status, json.loads(r.read() or b"{}")
            c.close()
            return out
        st, s = get("/api/state?local=1")
        self.assertEqual(st, 200)
        self.assertLess(len(s["chats"]), 60)
        self.assertTrue(s["chat_more"]["next"])
        st, pg = get("/api/chats?before=" + s["chat_more"]["next"].replace(" ", "%20").replace("|", "%7C"))
        self.assertEqual((st, len(pg["rows"])), (200, 50))
        st, ar = get("/api/chats?archived=1")
        self.assertEqual(sorted(r["id"] for r in ar["rows"]), ["w-005", "w-115"])


if __name__ == "__main__":
    unittest.main()
