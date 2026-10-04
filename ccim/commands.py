"""斜杠命令：ccim 自己处理的几个；其余原样交给 Claude（skill、/compact 都能用）。"""
import asyncio, re, time

from claude_agent_sdk import list_sessions

from . import registry
from .progress import clock

MODELS = ["opus", "sonnet", "haiku", "fable"]   # 简称交给本机 claude 对应到具体版本
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

HELP = """\
**直接发消息**就是在「{name}」项目里和 Claude 对话，项目里的 skill 和斜杠命令都能用。

/new　开始新对话
/resume　接上这个项目里的其他对话（包括在终端里开的）
/stop　停下正在做的事
/restart　重启这个机器人（升级到新代码时用）
/status　当前状态
/usage　额度和用量：5 小时、每周还剩多少
/model 名字　换模型：opus、sonnet、haiku、fable
/effort 等级　思考深度：low、medium、high、xhigh、max
/help　显示这段说明

也可以发图片和文件给我。"""


CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
CODEX_HELP = """\
**直接发消息**就是在「{name}」项目里和 Codex 对话。它干活时再发的消息，会等手头这件做完再处理。

/new　开始新对话
/stop　停下正在做的事
/restart　重启这个机器人
/status　当前状态
/model 名字　换模型
/effort 等级　思考深度：minimal、low、medium、high、xhigh
/help　显示这段说明

也可以发图片和文件给我。"""


def _model_name(m):
    return {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-4-5-20251001": "haiku",
            "claude-fable-5-1": "fable"}.get(m, m)


def _setting(chat):
    """「模型 · 思考深度」，没单独设置的标明跟随 Claude Code 设置。"""
    m, e, m_set, e_set = chat.effective()
    who = "Codex" if getattr(chat, "agent", None) == "codex" else "Claude Code"
    follow = f"（跟随 {who} 设置）" if not (m_set or e_set) else ""
    return f"**模型**：{_model_name(m)}　**思考深度**：{e}{follow}"


def _quota():
    from . import usage
    return usage.short_status()


def _title(info):
    t = info.custom_title or info.summary or info.first_prompt or "（空对话）"
    return " ".join(t.split())[:28]


async def _resume(bridge, chat, arg, say):
    sessions = await asyncio.to_thread(list_sessions, directory=bridge.path)
    feishu = {c.get("session_id") for c in (registry.read_state(bridge.path).get("chats") or {}).values()}
    sessions = [x for x in sessions if x.session_id != chat.session_id]
    if not arg:
        if not sessions:
            await say("这个项目里还没有别的对话。")
            return
        lines = ["**最近的对话**（发 /resume 编号 接上）："]
        for x in sessions[:8]:
            when = time.strftime("%m-%d %H:%M", time.localtime(x.last_modified / 1000))
            where = "飞书" if x.session_id in feishu else "终端"
            lines.append(f"`{x.session_id[:8]}`　{_title(x)}　{when} · {where}")
        await say("\n".join(lines))
        return
    hits = [x for x in sessions if x.session_id.startswith(arg.lower())]
    if not hits:
        await say(f"找不到编号以 {arg} 开头的对话，发 /resume 看看有哪些。")
    elif len(hits) > 1:
        await say(f"编号 {arg} 对应了 {len(hits)} 个对话，多写几位。")
    else:
        await chat.attach(hits[0].session_id)
        await say(f"已接上「{_title(hits[0])}」，之前的内容都在，接着说就行。原来那边的对话不受影响。")


async def handle(bridge, chat, text, reply_to, sender=None):
    """处理了就返回 True；不认识的命令返回 False，交给 Claude。"""
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    say = lambda s: bridge.channel.send_text(chat.chat_id, s, reply_to)

    codex = getattr(chat, "agent", None) == "codex"
    if codex and cmd in ("/resume", "/usage"):
        await say("这个项目用的是 Codex，没有这个功能。")
        return True
    if cmd == "/help":
        await say(HELP.format(name=bridge.name) if not codex else CODEX_HELP.format(name=bridge.name))
    elif cmd == "/new":
        await chat.reset()
        await say("已开始新对话。")
    elif cmd == "/resume":
        await _resume(bridge, chat, arg, say)
    elif cmd == "/restart":
        if sender != bridge.owner():
            await say("只有主人能重启。")
        else:
            from . import runtime
            await say("好，马上重启，大约半分钟后回来告诉你结果。新版本起不来的话，会先用稳定版顶上。")
            runtime.schedule_restart(bridge.path, delay=3, target=arg.lower() if arg.lower() in ("dev", "stable") else None)
    elif cmd == "/usage":
        from . import usage
        await say(await usage.report(bridge.path))
    elif cmd == "/stop":
        await say("正在停下。" if await chat.stop() else "现在没有在做的事。")
    elif cmd == "/status":
        if chat.turn:
            n = len(chat.turn.steps)
            state = f"工作中 {clock(time.monotonic() - chat.turn.start)}" + (f"，已做 {n} 步" if n else "")
        else:
            state = "空闲"
        waiting = sum(1 for item in chat.queue._queue if isinstance(item[0], str))
        lines = [f"**项目**：{bridge.name}" + ("（Codex）" if codex else ""),
                 f"**状态**：{state}" + (f"，还有 {waiting} 条排队" if waiting else ""),
                 _setting(chat),
                 *([f"**额度**：{q}"] if not codex and (q := _quota()) else []),
                 f"**对话**：{chat.session_id[:8] if chat.session_id else '新对话'}"]
        await say("\n".join(lines))
    elif cmd == "/model" and codex:
        if not arg:
            await say(f"现在用的是 {chat.effective()[0]}。要换的话发 /model 加模型名。")
        elif not re.fullmatch(r"[A-Za-z0-9._\-]+", arg):
            await say("模型名不对。")
        else:
            chat.set(model=arg)
            await say(f"好，接下来用 {arg}。")
    elif cmd == "/model":
        if not arg:
            await say(f"现在用的是 {_model_name(chat.effective()[0])}。可选：{'、'.join(MODELS)}，或者完整的模型名。")
        elif not re.fullmatch(r"[a-z0-9.\-\[\]]+", arg.lower()):
            await say("模型名不对。可选：" + "、".join(MODELS))
        else:
            chat.set(model=arg.lower() if arg.lower() in MODELS else arg)
            await say(f"好，接下来用 {_model_name(chat.model)}。")
    elif cmd == "/effort":
        levels = CODEX_EFFORTS if codex else EFFORTS
        if arg.lower() not in levels:
            await say(f"现在是 {chat.effective()[1]}。可选：{'、'.join(levels)}")
        else:
            chat.set(effort=arg.lower())
            await say(f"好，思考深度改成 {chat.effort}。")
    else:
        return False
    return True
