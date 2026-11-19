"""分叉后两边互相提醒（#507）：分出来的对话第一轮知道自己是分叉；原对话知道分叉改了哪些文件、跑了哪些命令。不跑真实对话。"""
import os
import unittest

from ccim import agents, platform
from tests import test_fork_marks as base


class FakeBridge:
    def __init__(self):
        self.chats, self.got = {}, []

    async def on_message(self, inc):
        self.got.append(inc.text)

    def chat(self, cid, kind, fork=None):
        self.chats[cid] = fork


class ForkNoteTests(base.ForkMarkTests):                              # 借它的 setUp 和 seed
    def setUp(self):
        super().setUp()
        self.bridge = FakeBridge()
        self.pf._bridge = lambda cid: self.bridge

    def send(self, cid, text):
        self.pf.run(self.pf.send(cid, text))
        return self.bridge.got[-1]

    def test_fork_told_once(self):
        cid, at = self.seed()
        a = self.pf.fork_chat(cid, 1, at=at + 10)["id"]
        first = self.send(a, "换个方向试试")
        self.assertIn("你是从「原对话」分出来的", first)
        self.assertIn("没做完的事归那边", first)
        self.assertIn("先问用户", first)
        self.assertTrue(first.endswith("换个方向试试"))
        self.assertNotIn("分出来的", self.send(a, "接着来"))            # 第二轮不再带

    def test_codex_fork_told(self):
        cid = self.pf.call(self.pf.new_chat, temp={"base": "codex"})
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "user", "id": "u1", "text": "问题"})
        logs.append(cid, {"type": "answer", "text": "答"})
        chats = self.pf.chats()
        chats[cid].update(codex_session_id="t1", title="Codex 的对话")
        self.pf.store.write(chats)
        a = self.pf.fork_chat(cid, 1)["id"]
        self.assertIn("你是从「Codex 的对话」分出来的", self.send(a, "接着"))

    def test_side_and_plain_chats_untouched(self):
        cid, at = self.seed()
        self.assertNotIn("分出来的对话", self.send(cid, "原对话里接着说"))
        s = self.pf.run(self.pf.side_chat(cid))
        self.assertNotIn("分出来的对话", self.send(s, "侧边问一句"))
        self.assertNotIn("fork_note", self.pf.meta(s))

    def test_parent_sees_fork_files_once(self):
        cid, at = self.seed()
        logs = self.pf.channel.logs
        # 原对话自己分叉前改过的文件：复制到分叉里，不能当成分叉改的
        logs.append(cid, {"type": "user", "id": "u0", "text": "先改个", "at": at - 100})
        a = self.pf.fork_chat(cid, 3, at=at + 30)["id"]
        quiet = self.pf.fork_chat(cid, 1, at=at + 10)["id"]          # 只聊天的分叉
        chats = self.pf.chats()
        chats[a]["title"] = "换个方向"
        self.pf.store.write(chats)
        self.assertEqual(self.pf._side_news(cid), "")                 # 刚分出来，什么都没做
        cwd = agents.cwd(agents.get(self.aid))
        logs.append(a, {"type": "user", "id": "u9", "text": "改一下"})
        logs.append(a, {"type": "answer", "text": "改好了", "steps": ["修改 a.md", "运行：npm test"],
                        "changed": [{"path": os.path.join(cwd, "src", "a.md")}]})
        logs.append(quiet, {"type": "user", "id": "u8", "text": "只是问问"})
        logs.append(quiet, {"type": "answer", "text": "好的", "steps": ["读取 b.md"]})
        got = self.send(cid, "回到原对话")
        self.assertIn("分叉「换个方向」：改了 src/a.md；跑了 npm test", got)
        self.assertIn("分叉出去的对话", got)
        self.assertNotIn("侧边对话", got)
        self.assertEqual(got.count("分叉「"), 1)                      # 只聊天的分叉什么都不附
        self.assertNotIn("改了", self.send(cid, "再说一句"))            # 同一批只附一次
        logs.append(a, {"type": "answer", "text": "又改了", "changed": [{"path": os.path.join(cwd, "c.md")}]})
        got = self.pf._side_news(cid)
        self.assertIn("c.md", got)
        self.assertNotIn("a.md", got)

    def test_old_fork_starts_from_now(self):
        """以前分的叉没记复制了几条：从现在算起，不把复制过去的原对话内容当成分叉做的。"""
        cid, at = self.seed()
        logs = self.pf.channel.logs
        logs.append(cid, {"type": "answer", "text": "原对话改的", "changed": [{"path": "/x/old.md"}]})
        a = self.pf.fork_chat(cid, 4)["id"]
        chats = self.pf.chats()
        chats[a].pop("fork_upto")
        self.pf.store.write(chats)
        self.assertEqual(self.pf._side_news(cid), "")

    def test_dispatched_work_not_counted_as_fork(self):
        cid, at = self.seed()
        new = self.pf.new_chat(agent=self.aid, title="派出的任务", parent=cid)
        chats = self.pf.chats()
        chats[new]["dispatched_from"] = cid
        self.pf.store.write(chats)
        self.pf.channel.logs.append(new, {"type": "answer", "text": "做完", "changed": [{"path": "/x/y.md"}]})
        self.assertEqual(self.pf._side_news(cid), "")


if __name__ == "__main__":
    unittest.main()
