"""把渠道收到的消息分给各个聊天的 Claude 会话；谁能用、群里怎么用都在这里判断。"""
import asyncio, logging, os

from . import commands, registry
from .approval import Approvals
from .channels.base import address
from .session import Caffeinate, Chat

log = logging.getLogger("ccim.bridge")


class Bridge:
    def __init__(self, path, entry):
        self.path, self.entry = path, entry
        self.name = os.path.basename(path)
        self.channel = None
        self.approvals = None
        self.chats = {}
        self.background = set()               # 后台补发等任务，留着引用免得被回收
        self.caffeinate = Caffeinate()
        self.inbox = os.path.join(path, ".ccim", "inbox")

    def attach(self, channel):
        self.channel = channel
        self.approvals = Approvals(channel, self.name)

    def handlers(self):
        return {"on_message": self.on_message, "on_action": self.on_action, "on_bot_added": self.on_bot_added}

    # ----- 每个聊天的会话记录，存在 state.json 的 chats 里 -----

    def saved(self, chat_id):
        return (registry.read_state(self.path).get("chats") or {}).get(chat_id, {})

    def save(self, chat_id, **changes):
        chats = registry.read_state(self.path).get("chats") or {}
        chats.setdefault(chat_id, {}).update(changes)
        registry.write_state(self.path, chats=chats)

    def chat(self, addr, chat_type, fork_from=None):
        c = self.chats.get(addr)
        if not c:
            c = self.chats[addr] = Chat(self, addr, chat_type, fork_from)
            if chat_type == "group" and not self.saved(addr).get("name"):
                task = asyncio.create_task(self._remember_name(addr))   # 群名给 ccim show 用
                self.background.add(task)
                task.add_done_callback(self.background.discard)
        return c

    async def _remember_name(self, addr):
        name = await self.channel.chat_name(addr.split("|")[0])
        if name:
            self.save(addr, name=name)

    # ----- 权限 -----

    def owner(self):
        return self.entry.get("owner_open_id")

    def allowed(self, sender_id, chat_type):
        if sender_id and sender_id == self.owner():
            return True
        return chat_type == "group" and self.entry.get("group") == "all"

    # ----- 事件 -----

    async def on_message(self, inc):
        text = (inc.text or "").strip()
        if self.entry.get("claim_code") and inc.chat_type == "p2p":
            if text == self.entry["claim_code"]:
                self.entry = registry.update(self.path, owner_open_id=inc.sender_id, claim_code=None)
                log.info("主人已认领：%s", inc.sender_id)
                await self.channel.send_text(inc.chat_id, "好了，以后我只听你的。直接发消息就能开始。")
            return
        if not self.allowed(inc.sender_id, inc.chat_type):
            log.info("忽略 %s 在 %s 的消息（不是主人）", inc.sender_id, inc.chat_type)
            return
        if inc.chat_type == "group" and not inc.mentioned_bot:
            return
        # 话题里的消息：回复都进话题；话题第一次开口时从主对话分一个分支出来，记得之前聊过什么
        addr = address(inc.chat_id, inc.thread_id)
        reply_to = inc.message_id if inc.chat_type == "group" or inc.thread_id else None
        new_topic = bool(inc.thread_id) and addr not in self.chats and not self.saved(addr).get("session_id")
        main = self.chats.get(inc.chat_id)
        fork_from = (main.session_id if main else self.saved(inc.chat_id).get("session_id")) if new_topic else None
        chat = self.chat(addr, inc.chat_type, fork_from)

        paths = []
        for kind, key, fname in inc.files:
            if not key:
                continue
            try:
                os.makedirs(self.inbox, exist_ok=True)
                ignore = os.path.join(self.inbox, "..", ".gitignore")
                if not os.path.exists(ignore):
                    with open(ignore, "w") as f:
                        f.write("*\n")
                paths.append(await self.channel.download(inc.message_id, kind, key, fname, self.inbox))
            except Exception as e:
                log.warning("下载文件失败", exc_info=True)
                await self.channel.send_text(addr, f"文件没收到：{e}", reply_to)
        if not text and not paths:
            if inc.kind not in ("text", "post", "image", "file", "media", "audio"):
                await self.channel.send_text(addr, "这类消息我还看不了，请发文字、图片或文件。", reply_to)
            return
        log.info("← [%s%s] %s%s", "私聊" if inc.chat_type == "p2p" else "群聊", "·话题" if inc.thread_id else "", text[:80],
                 f"（附件 {len(paths)} 个）" if paths else "")

        if text and not paths and self.approvals.has_pending(addr):
            if await self.approvals.answer_by_text(addr, text):
                return
        if text.startswith("/") and not paths:
            if await commands.handle(self, chat, text, reply_to):
                return
        prompt = text
        if new_topic and inc.root_id and inc.root_id != inc.message_id:
            root = await self.channel.get_text(inc.root_id)
            if root:
                prompt = f"（用户针对下面这条消息开了一个话题，接下来在话题里讨论它）\n「{root[:800]}」\n\n{prompt}"
        if paths:
            prompt += ("\n\n" if prompt else "") + "用户发来了文件，已保存到：\n" + "\n".join(paths)
        ahead = chat.submit(prompt, reply_to, inc.message_id)
        if ahead > 0:
            await self.channel.send_text(addr, f"收到，前面还有 {ahead} 条在处理，轮到它再开始。", reply_to)

    async def on_action(self, act):
        v = act.value or {}
        if v.get("ccim") != "approve":
            return None, None
        if act.operator_id != self.owner():
            return None, "只有机器人的主人能批准"
        card = self.approvals.resolve(v.get("id"), v.get("choice"))
        return (card, None) if card else (None, "这个请求已经处理过了")

    async def on_bot_added(self, chat_id):
        log.info("被拉进群 %s", chat_id)
        chat = self.chats.pop(chat_id, None)
        if chat:
            await chat.reset()
        self.save(chat_id, session_id=None)
        tip = "@我 就能让我干活。" if self.entry.get("group") == "all" else "我只响应主人的 @。"
        await self.channel.send_text(chat_id, f"我是「{self.name}」项目的 Claude。{tip}发 /help 看看能做什么。")

    async def shutdown(self):
        for c in list(self.chats.values()):
            await c.stop()
            if c.worker:
                c.worker.cancel()
        await asyncio.sleep(0.2)
