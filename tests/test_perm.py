"""对话里的权限（#396）：被拦下的操作弹卡片放行、这一轮都允许、全部放行、做完切回自动；不跑真实对话。"""
import asyncio
import unittest
from types import SimpleNamespace

from ccim.approval import Approvals
from ccim.session import Chat


class FakeChannel:
    def __init__(self):
        self.cards = []

    async def send_card(self, chat_id, card):
        self.cards.append(card)
        return f"m{len(self.cards)}"

    async def update_card(self, mid, card):
        self.cards.append(card)


class FakeClient:
    def __init__(self):
        self.modes = []

    async def set_permission_mode(self, mode):
        self.modes.append(mode)


def make_chat(entry=None):
    store = {}
    b = SimpleNamespace(entry=entry or {}, path="/tmp", channel=FakeChannel(),
                        saved=lambda cid: store.get(cid, {}),
                        save=lambda cid, **kw: store.setdefault(cid, {}).update(kw))
    b.approvals = Approvals(b.channel, "测试")
    c = object.__new__(Chat)
    c.b, c.chat_id, c.client = b, "w-1", None
    c.perm, c.perm_once, c.turn_free, c.allow_once = None, True, False, []
    return c, store


BASH = {"tool_name": "Bash", "tool_input": {"command": "git push origin --delete old"}}


class PermTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, c, choice):
        while not c.b.approvals.pending:
            await asyncio.sleep(0.01)
        pid = next(iter(c.b.approvals.pending))
        c.b.approvals.resolve(pid, choice)

    async def test_allow_once_lets_identical_retry_through(self):
        c, _ = make_chat()
        self.assertEqual(await c._pre_tool(BASH, "t1", None), {})
        r, _ = await asyncio.gather(c._denied(BASH, "t1", None), self.click(c, "allow"))
        self.assertTrue(r["hookSpecificOutput"]["retry"])
        self.assertIn("这一轮都允许", [b["text"] for b in c.b.channel.cards[0]["buttons"]])
        other = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        self.assertEqual(await c._pre_tool(other, "t2", None), {})          # 别的命令不放行
        ok = await c._pre_tool(BASH, "t3", None)
        self.assertEqual(ok["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(await c._pre_tool(BASH, "t4", None), {})           # 只放行一次

    async def test_turn_allows_everything_until_turn_end(self):
        c, store = make_chat()
        await asyncio.gather(c._denied(BASH, "t1", None), self.click(c, "turn"))
        self.assertTrue(store["w-1"]["turn_free"])
        other = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}}
        self.assertEqual((await c._pre_tool(other, "t2", None))["hookSpecificOutput"]["permissionDecision"], "allow")
        await c._perm_turn_end()
        self.assertFalse(c.turn_free)
        self.assertFalse(store["w-1"]["turn_free"])
        self.assertEqual(await c._pre_tool(other, "t3", None), {})

    async def test_deny_does_not_retry(self):
        c, _ = make_chat()
        r, _ = await asyncio.gather(c._denied(BASH, "t1", None), self.click(c, "deny"))
        self.assertNotIn("retry", r["hookSpecificOutput"])
        self.assertEqual(await c._pre_tool(BASH, "t2", None), {})

    async def test_free_and_switch_back_after_turn(self):
        c, store = make_chat()
        c.client = FakeClient()
        await c.set_perm(perm="ask")
        self.assertEqual(c.client.modes, ["default"])
        await c.set_perm(perm="free")
        self.assertEqual(c.client.modes[-1], "auto")
        self.assertEqual((await c._pre_tool(BASH, "t1", None))["hookSpecificOutput"]["permissionDecision"], "allow")
        await c._perm_turn_end()
        self.assertIsNone(c.perm)
        self.assertIsNone(store["w-1"]["perm"])
        await c.set_perm(perm="free")
        await c.set_perm(perm=False, once=False)
        await c._perm_turn_end()
        self.assertEqual(c.perm, "free")                                    # 关了「切回自动」就一直开着

    async def test_ccim_own_block_still_wins(self):
        c, _ = make_chat()
        c.perm = "free"
        called = []

        async def pre_bash(data, tid, ctx):
            called.append(1)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
        c._pre_bash = pre_bash
        r = await c._pre_tool(BASH, "t1", None)
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_guest_side_lent_cannot(self):
        for entry in ({"guest": "acc"}, {"side": True}, {"lent": True}, {"helper": "skill"}):
            c, _ = make_chat(entry)
            c.perm = "free"
            await c.set_perm(perm="free")
            self.assertEqual(await c._pre_tool(BASH, "t1", None), {})
            self.assertEqual(await c._denied(BASH, "t1", None), {})
            self.assertFalse(c.b.channel.cards)


if __name__ == "__main__":
    unittest.main()
