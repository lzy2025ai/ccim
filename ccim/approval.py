"""can_use_tool → 审批卡片 → 等主人点按钮或回复 y / n。

auto 模式下，大多数操作由 Claude Code 自己放行或拦下，不会走到这里；
走到这里的是它认为必须由人拍板的少数操作。
"""
import asyncio, itertools, logging

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from .progress import describe_request

log = logging.getLogger("ccim.approval")

TIMEOUT = 600
YES = {"y", "yes", "允许", "同意", "可以", "好", "ok"}
NO = {"n", "no", "拒绝", "不行", "不要", "不"}
_ids = itertools.count(1)


class Pending:
    def __init__(self, chat_id, name, inp, ctx):
        self.id = f"a{next(_ids)}"
        self.chat_id, self.name, self.inp, self.ctx = chat_id, name, inp, ctx
        self.future = asyncio.get_running_loop().create_future()
        self.message_id = None

    def body(self):
        parts = [describe_request(self.name, self.inp)]
        why = getattr(self.ctx, "description", None) or getattr(self.ctx, "decision_reason", None)
        if why:
            parts.append(f"**原因**：{why}")
        return "\n\n".join(parts)

    def card(self, project):
        buttons = [{"text": "允许", "style": "primary", "value": {"ccim": "approve", "id": self.id, "choice": "allow"}}]
        if getattr(self.ctx, "suggestions", None):
            buttons.append({"text": "本会话都允许", "style": "default",
                            "value": {"ccim": "approve", "id": self.id, "choice": "always"}})
        buttons.append({"text": "拒绝", "style": "danger", "value": {"ccim": "approve", "id": self.id, "choice": "deny"}})
        return {"title": f"需要你批准 · {project}", "color": "orange", "body": self.body(), "buttons": buttons,
                "note": "也可以直接回复 y 或 n"}

    def resolved_card(self, project, choice):
        title, color = {"allow": ("已允许", "green"), "always": ("已允许（本会话同类操作不再询问）", "green"),
                        "deny": ("已拒绝", "red"), "timeout": ("超时未处理，已拒绝", "grey"),
                        "cancel": ("已取消", "grey")}[choice]
        return {"title": f"{title} · {project}", "color": color, "body": self.body()}


class Approvals:
    def __init__(self, channel, project):
        self.channel, self.project = channel, project
        self.pending = {}                     # id → Pending

    async def ask(self, chat_id, name, inp, ctx):
        p = Pending(chat_id, name, inp, ctx)
        self.pending[p.id] = p
        try:
            p.message_id = await self.channel.send_card(chat_id, p.card(self.project))
            choice = await asyncio.wait_for(asyncio.shield(p.future), TIMEOUT)
        except asyncio.TimeoutError:
            choice = "timeout"
        except asyncio.CancelledError:
            await self._update(p, "cancel")
            raise
        finally:
            self.pending.pop(p.id, None)
        log.info("审批 %s %s → %s", name, str(inp)[:80], choice)
        if choice in ("timeout", "cancel"):           # 按钮、文字回复的卡片在各自的路径里已经更新过了
            await self._update(p, choice)
        if choice == "allow":
            return PermissionResultAllow()
        if choice == "always":
            return PermissionResultAllow(updated_permissions=ctx.suggestions)
        return PermissionResultDeny(message={"deny": "用户拒绝了这个操作。", "cancel": "用户停止了这个任务。"}.get(
            choice, "用户没有及时批准，已当作拒绝。"))

    async def _update(self, p, choice):
        if p.message_id:
            try:
                await self.channel.update_card(p.message_id, p.resolved_card(self.project, choice))
            except Exception:
                log.warning("更新审批卡片失败", exc_info=True)

    def resolve(self, pid, choice):
        """按钮回调：返回更新后的卡片；已经处理过的返回 None。"""
        p = self.pending.get(pid)
        if not p or p.future.done():
            return None
        p.future.set_result(choice)
        return p.resolved_card(self.project, choice)

    async def answer_by_text(self, chat_id, text):
        """主人直接回复 y / n：处理这个聊天里最早的一张待审批卡片。返回是否处理了。"""
        t = text.strip().lower().rstrip("。.!！")
        choice = "allow" if t in YES else "deny" if t in NO else None
        if not choice:
            return False
        for p in self.pending.values():
            if p.chat_id == chat_id and not p.future.done():
                p.future.set_result(choice)
                await self._update(p, choice)
                return True
        return False

    def has_pending(self, chat_id):
        return any(p.chat_id == chat_id and not p.future.done() for p in self.pending.values())

    def cancel_chat(self, chat_id):
        """/stop、/new 时撤掉这个聊天里没处理的审批：卡片由 ask 改成「已取消」。"""
        for p in list(self.pending.values()):
            if p.chat_id == chat_id and not p.future.done():
                p.future.set_result("cancel")
