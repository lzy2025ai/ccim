"""@ 正在干活的成员时补充进它这一轮（#304）；不跑真实对话。"""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from ccim.mentions import Mentions
from ccim.platform import Logs, Platform
from ccim.session import Task


class FakeChat:
    def __init__(self, accept=True):
        self.current = None
        self.queue = asyncio.Queue()
        self.turn = SimpleNamespace(steps=[])
        self.accept = accept
        self.got = []

    async def inject(self, prompt, reply_to=None):
        if not self.accept:
            return False
        self.got.append(prompt)
        self.turn.steps.append("收到补充：" + prompt[:40])
        return True


class InjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.logs = Logs(self.dir.name)
        self.meta = {"w-a": {}}
        self.pf = object.__new__(Platform)
        self.pf.channel = SimpleNamespace(logs=self.logs, progress_of={}, _emit=lambda cid, rec: None)
        self.pf.chats = lambda: self.meta
        self.pf.store = SimpleNamespace(write=lambda chats: None)
        self.pf.background = set()
        self.chat = FakeChat()
        self.pf.bridges = {"b": SimpleNamespace(chats={Mentions.sub("w-a", "ccim"): self.chat})}
        self.m = Mentions(self.pf)

    def tearDown(self):
        self.dir.cleanup()

    def running(self):
        sub = Mentions.sub("w-a", "ccim")
        task = Task("先做这个")
        self.m.tasks["u-1:ccim"] = (sub, task)
        self.m.running.add("u-1:ccim")
        self.chat.current = (task, None)
        return task

    def mention(self, rid):
        return next(r for r in self.logs.read("w-a") if r.get("type") == "mention" and r["id"] == rid)

    async def test_busy_member_gets_it_in_this_turn(self):
        self.running()
        rid = await self.m._inject("w-a", "ccim", "u-2", "[用户在对话里 @ 了你] 标题别改了", "标题别改了", 5, 0)
        self.assertEqual(rid, "u-1:ccim")
        self.assertEqual(self.chat.got, ["[用户在对话里 @ 了你] 标题别改了"])
        self.assertEqual(self.chat.turn.steps[-1], "收到补充：标题别改了")
        self.assertEqual(self.mention("u-1:ccim")["joined"], ["u-2"])
        sub_rows = self.logs.read(Mentions.sub("w-a", "ccim"))
        self.assertEqual(sub_rows[-1]["text"], "标题别改了")

    async def test_idle_or_other_work_falls_back_to_queue(self):
        self.assertIsNone(await self.m._inject("w-a", "ccim", "u-2", "p", "t", 5, 0))
        self.running()
        self.chat.current = (Task("别的活"), None)  # 正在做的不是这个对话交给它的
        self.assertIsNone(await self.m._inject("w-a", "ccim", "u-2", "p", "t", 5, 0))
        self.running()
        self.chat.accept = False                   # 刚好做完
        self.assertIsNone(await self.m._inject("w-a", "ccim", "u-2", "p", "t", 5, 0))

    async def test_absorbed_supplement_adds_nothing(self):
        self.running()
        await self.m._inject("w-a", "ccim", "u-2", "p", "t", 5, 0)
        self.m._unjoin("w-a", "u-1:ccim", Mentions.sub("w-a", "ccim"))
        self.assertEqual([r["id"] for r in self.logs.read("w-a") if r.get("type") == "mention"], ["u-1:ccim"])
        self.assertEqual(self.mention("u-1:ccim")["joined"], ["u-2"])
        self.assertNotIn("u-1:ccim", self.m.joined)

    async def test_late_supplement_gets_its_own_answer(self):
        self.running()
        await self.m._inject("w-a", "ccim", "u-2", "p", "t", 5, 0)
        late = self.m.joined["u-1:ccim"][0][2]
        self.chat.queue.put_nowait((late, None, None))   # Claude 没接着处理，排成了单独一轮
        self.m._unjoin("w-a", "u-1:ccim", Mentions.sub("w-a", "ccim"))
        self.assertEqual(self.mention("u-1:ccim")["joined"], [])
        self.assertEqual(self.mention("u-2:ccim")["state"], "queued")
        self.assertIn("u-2:ccim", self.m.running)
        late.finish("ok", "改好了")
        await asyncio.gather(*self.pf.background)
        r = self.mention("u-2:ccim")
        self.assertEqual((r["state"], r["text"], r["msg"]), ("done", "改好了", "u-2"))
        self.assertNotIn("u-2:ccim", self.m.running)


if __name__ == "__main__":
    unittest.main()
