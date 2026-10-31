"""侧边对话里 @ 其他 Agent（#290）：被 @ 的只读、带上主对话内容、不进主对话；新开对话去做时侧边栏有卡片。不跑真实对话。"""
import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccim import agents, registry, share


class SideMentionTests(unittest.TestCase):
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
        entry = {"kind": "platform", "channel": "web", "port": 18978}
        pf = self.pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        self.main_agent = agents.create(name="写作助手")["id"]
        self.helper = agents.create(name="开发助手")["id"]
        self.member = agents.create(name="设计助手")["id"]
        self.tasks = []
        real = pf._bridge

        def bridge(cid):
            b = real(cid)                     # 真的建 Bridge，看它的设置；交活的地方换成记下来
            return SimpleNamespace(entry=b.entry, chat=lambda c, t: SimpleNamespace(submit=lambda task: self.tasks.append((c, task))),
                                   save=b.save, chats={})
        pf._bridge = bridge
        self.real_bridge = real

    def group(self):
        pf = self.pf
        cid = pf.call(pf.new_chat, agent=self.main_agent, title="写公众号")
        chats = pf.chats()
        chats[cid]["members"] = [self.member]
        pf.store.write(chats)
        logs = pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": "帮我写一篇开头"})
        logs.append(cid, {"type": "answer", "text": "开头写好了：秋天来了"})
        logs.append(cid, {"type": "mention", "id": "u2:" + self.member, "agent": self.member, "state": "done",
                          "text": "配图建议用落叶", "done_at": 1})
        return cid

    def test_mention_in_side_is_read_only_and_stays_out_of_main(self):
        pf = self.pf
        main = self.group()
        before = len(pf.channel.logs.read(main))
        side = pf.run(pf.side_chat(main))
        pf.run(pf.mentions.send(side, "这个开头有什么问题", [self.helper]))
        self.assertEqual(pf.chats()[main].get("members"), [self.member])    # 没被拉进主对话的群
        self.assertFalse(pf.chats()[side].get("members"))
        self.assertEqual(len(pf.channel.logs.read(main)), before)           # 主对话记录没动
        kinds = [r["type"] for r in pf.channel.logs.read(side)]
        self.assertEqual(kinds, ["user", "mention"])
        for _ in range(50):
            if self.tasks:
                break
            asyncio.run(asyncio.sleep(0.02))
        sub, task = self.tasks[0]
        self.assertEqual(sub, f"m-{side}-{self.helper}")
        self.assertIn("秋天来了", str(task))                               # 主对话的内容
        self.assertIn("配图建议用落叶", str(task))                           # 群里成员的回答
        self.assertIn("侧边对话里 @ 了你", str(task))
        m = pf.chats()[sub]
        self.assertTrue(m.get("side_at"))
        e = self.real_bridge(sub).entry
        self.assertTrue(e.get("side") and e.get("side_at"))
        self.assertIn("start_work", e["role"])

        # 第二次 @：主对话没有新内容就不再带
        pf.run(pf.mentions.send(side, "那结尾呢", [self.helper]))
        for _ in range(50):
            if len(self.tasks) > 1:
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertNotIn("秋天来了", str(self.tasks[1][1]))

        # 主对话接着聊：不会把侧边栏里的 @ 回答拼给主 Agent
        pf.channel.logs.append(side, {"type": "mention", "id": "zz:" + self.helper, "agent": self.helper, "state": "done",
                                      "text": "侧边栏里的回答", "done_at": 9e9})
        self.assertNotIn("侧边栏里的回答", pf.mentions.unseen(main))

    def test_start_work_card(self):
        pf = self.pf
        main = self.group()
        side = pf.run(pf.side_chat(main))
        sub = pf.mentions._ensure(side, self.helper, None, "写公众号", side=True)
        cid, title = pf.run(self._work(sub, "把开头改短一点"))
        self.assertEqual(title, "把开头改短一点")
        m = pf.chats()[cid]
        self.assertEqual(m["agent"], self.helper)
        self.assertEqual(m["dispatched_from"], main)
        self.assertNotIn("parent", {k for k, v in m.items() if v})            # 别的 Agent：不挂在主对话下面
        card = [r for r in pf.channel.logs.read(side) if r["type"] == "work"]
        self.assertEqual(card[-1]["state"], "working")
        self.assertEqual(pf.chat_detail(side)["log"][-1]["state"], "working")
        task = [t for c, t in self.tasks if c == cid][0]
        pf.channel.logs.append(cid, {"type": "file", "path": "/tmp/a.md", "name": "a.md", "size": 3})
        pf.loop.call_soon_threadsafe(task.finish, "ok", "改好了，开头缩到两句")
        for _ in range(50):
            card = [r for r in pf.channel.logs.read(side) if r["type"] == "work"]
            if card[-1]["state"] != "working":
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertEqual(card[-1]["state"], "done")
        self.assertNotIn("text", card[-1])                                     # 卡片只管进度
        # 结果交回它在侧边对话里的会话，由它自己汇报
        for _ in range(50):
            back = [t for c, t in self.tasks if c == sub]
            if back:
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertIn("改好了，开头缩到两句", str(back[0]))
        self.assertIn("a.md", str(back[0]))
        pf.loop.call_soon_threadsafe(back[0].finish, "ok", "开头改成两句了，附上改好的稿子，你看一下")
        for _ in range(50):
            rep = [r for r in pf.channel.logs.read(side) if r["type"] == "mention" and r.get("report")]
            if rep and rep[-1]["state"] != "queued":
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertEqual(rep[-1]["state"], "done")
        self.assertEqual(rep[-1]["text"], "开头改成两句了，附上改好的稿子，你看一下")
        self.assertEqual([f["name"] for f in rep[-1]["files"]], ["a.md"])
        self.assertEqual(rep[-1]["files_chat"], cid)
        main_types = [r["type"] for r in pf.channel.logs.read(main)]
        self.assertNotIn("work", main_types)
        self.assertEqual(main_types.count("mention"), 1)                       # 主对话里只有原来那条成员回答
        self.assertNotIn("开头改成两句了", pf.mentions.unseen(main))

    async def _work(self, sub, text):
        return self.pf.work(sub, text)

    def test_only_mentioned_can_start_work(self):
        pf = self.pf
        main = self.group()
        from ccim.platform import Problem
        with self.assertRaises(Problem):
            pf.run(self._work(main, "改一下"))
        far = pf.mentions._ensure(main, self.helper, None, "写公众号")
        chats = pf.chats()
        chats[far]["remote_project"] = True                                    # 别的电脑派来的：先不做
        pf.store.write(chats)
        with self.assertRaises(Problem):
            pf.run(self._work(far, "改一下"))

    def test_group_member_start_work(self):
        """群聊里被 @ 的成员（#359）：新开它自己的对话，卡片记在群里，做完回群里汇报、文件附在汇报下面。"""
        pf = self.pf
        main = self.group()
        sub = pf.mentions._ensure(main, self.member, None, "写公众号")
        cid, title = pf.run(self._work(sub, "出三张配图"))
        m = pf.chats()[cid]
        self.assertEqual(m["agent"], self.member)
        self.assertEqual(m["dispatched_from"], main)
        self.assertFalse(m.get("parent"))
        card = [r for r in pf.channel.logs.read(main) if r["type"] == "work"]
        self.assertEqual((card[-1]["state"], card[-1]["chat"]), ("working", cid))
        task = [t for c, t in self.tasks if c == cid][0]
        self.assertIn("群聊「写公众号」", str(task))
        pf.channel.logs.append(cid, {"type": "file", "path": "/tmp/p.png", "name": "p.png", "size": 3})
        pf.loop.call_soon_threadsafe(task.finish, "ok", "三张图做好了")
        for _ in range(50):
            back = [t for c, t in self.tasks if c == sub]
            if back:
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertIn("请在群聊里", str(back[0]))
        pf.loop.call_soon_threadsafe(back[0].finish, "ok", "配图好了，见下面")
        for _ in range(50):
            rep = [r for r in pf.channel.logs.read(main) if r["type"] == "mention" and r.get("report")]
            if rep and rep[-1]["state"] != "queued":
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertEqual(rep[-1]["text"], "配图好了，见下面")
        self.assertEqual(rep[-1]["files_chat"], cid)
        self.assertEqual([r["state"] for r in pf.channel.logs.read(main) if r["type"] == "work"][-1], "done")
        self.assertIn("配图好了", pf.mentions.unseen(main))                    # 主 Agent 下次能看到成员的汇报

    def test_agent_list_shows_group_itself(self):
        """Agent 页的列表（#359）：它在的群聊列群聊本身，只是成员的标 member；侧边对话里被 @ 的不列。"""
        from ccim.platform import _agent_chats
        pf = self.pf
        main = self.group()
        sub = pf.mentions._ensure(main, self.member, None, "写公众号")
        side = pf.run(pf.side_chat(main))
        pf.mentions._ensure(side, self.member, None, "写公众号", side=True)
        rows = {r["id"]: r for r in _agent_chats(pf, self.member)}
        self.assertIn(main, rows)
        self.assertTrue(rows[main]["member"])
        self.assertNotIn(sub, rows)
        self.assertFalse(any(r.get("mention_of") for r in rows.values()))
        own = {r["id"]: r for r in _agent_chats(pf, self.main_agent)}
        self.assertIn(main, own)
        self.assertFalse(own[main].get("member"))

    def test_tools(self):
        from ccim import session
        pf = self.pf
        main = self.group()
        sub = pf.mentions._ensure(main, self.member, None, "写公众号")
        side = pf.run(pf.side_chat(main))
        sat = pf.mentions._ensure(side, self.helper, None, "写公众号", side=True)
        def chat(cid):
            return SimpleNamespace(chat_id=cid, b=SimpleNamespace(entry=self.real_bridge(cid).entry))
        self.assertTrue(session._can_work(chat(sub), pf))
        self.assertTrue(session._can_work(chat(sat), pf))
        self.assertFalse(session._can_work(chat(main), pf))
        self.assertFalse(session._can_work(chat(side), pf))

    def test_remote_agent_on_old_version(self):
        pf = self.pf
        main = self.group()
        side = pf.run(pf.side_chat(main))
        pf.peers.agent_home = lambda aid: "far"
        pf.peers.cache["far"] = {"agents": [{"id": "x1", "name": "远处的助手"}]}
        from ccim.platform import Problem
        with self.assertRaisesRegex(Problem, "升级"):
            pf.run(pf.mentions.send(side, "看看", ["x1"]))


if __name__ == "__main__":
    unittest.main()
