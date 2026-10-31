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
/skip　某一步卡住了：结束这一步，接着做
/queue 内容　排在后面：手头这件做完再处理（不加的话，干活时发的消息会直接补充进去）
/diary 内容　记一笔日记，不打断手头的事
/restart　重启这个机器人（升级到新代码时用）
/status　当前状态
/usage　额度和用量：5 小时、每周还剩多少
/model 名字　换模型：opus、sonnet、haiku、fable；/model 默认 回到默认
/effort 等级　思考深度：low、medium、high、xhigh、max；/effort 默认 回到默认
/help　显示这段说明

也可以发图片和文件给我。"""


CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
CODEX_HELP = """\
**直接发消息**就是在「{name}」项目里和 Codex 对话。它干活时再发的消息，会等手头这件做完再处理。

/new　开始新对话
/stop　停下正在做的事
/diary 内容　记一笔日记，不打断手头的事
/restart　重启这个机器人
/status　当前状态
/model 名字　换模型；/model 默认 回到默认
/effort 等级　思考深度：minimal、low、medium、high、xhigh；/effort 默认 回到默认
/help　显示这段说明

也可以发图片和文件给我。"""


def _model_name(m):
    return {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-4-5-20251001": "haiku",
            "claude-fable-5-1": "fable"}.get(m, m)


def _ctx(c):
    """上下文占用：已用 / 上限（百分比）。"""
    return f"{_k(c['used'])} / {_k(c['max'])} token（{round(c.get('pct') or 0)}%）"


def _k(n):
    if n >= 1000000:
        return f"{n / 1000000:.1f}M".replace(".0M", "M")
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n < 100000 else f"{round(n / 1000)}k"


def _setting(chat):
    """「模型 · 思考深度」，没单独设置的标明跟随 Claude Code 设置。"""
    m, e, m_set, e_set = chat.effective()
    who = "Codex" if getattr(chat, "agent", None) == "codex" else "Claude Code"
    follow = f"（跟随 {who} 设置）" if not (m_set or e_set) else ""
    return f"**模型**：{_model_name(m)}　**思考深度**：{e}{follow}"


async def _quota(path):
    from . import usage
    if usage.stale():                         # 缓存只在对话跑过一轮时更新，旧了先刷新
        await usage.refresh(path)
    return usage.short_status()


def _title(info):
    t = info.custom_title or info.summary or info.first_prompt or "（空对话）"
    return " ".join(t.split())[:28]


async def _resume(bridge, chat, arg, say):
    sessions = await asyncio.to_thread(list_sessions, directory=bridge.path)
    where_of = {c.get("session_id"): where for where, _, c in registry.all_chats(bridge.path)}
    sessions = [x for x in sessions if x.session_id != chat.session_id]
    if not arg:
        if not sessions:
            await say("这个项目里还没有别的对话。")
            return
        lines = ["**最近的对话**（发 /resume 编号 接上）："]
        for x in sessions[:8]:
            when = time.strftime("%m-%d %H:%M", time.localtime(x.last_modified / 1000))
            where = where_of.get(x.session_id, "终端")
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

    from . import diary
    note = diary.parse(text)
    if note is not None:                      # /diary（#363）：记进日记，不交给助手
        if sender != bridge.owner():
            await say("只有主人能记日记。")
            return True
        try:
            r = diary.add(note, f"{bridge.channel.label or '聊天'} · {bridge.name}")
        except ValueError as err:
            await say(str(err))
        except OSError:
            await say("日记没记上：文稿文件夹写不进去。")
        else:
            await say(f"已记到 {r['date']} 的日记（{r['time']}）。")
        return True

    codex = getattr(chat, "agent", None) == "codex"
    if codex and cmd in ("/resume", "/usage"):
        await say("这个项目用的是 Codex，没有这个功能。")
        return True
    if cmd == "/help":
        await say((HELP if not codex else CODEX_HELP).format(name=bridge.name) + bridge.help_extra)
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
            runtime.schedule_restart(bridge.home, delay=3, target=arg.lower() if arg.lower() in ("dev", "stable") else None)
    elif cmd == "/usage":
        from . import usage
        await say(await usage.report(bridge.path))
    elif cmd == "/stop":
        await say("正在停下。" if await chat.stop() else "现在没有在做的事。")
    elif cmd in ("/skip", "/跳过"):
        if codex:
            await say("这个项目用的是 Codex，没有这个功能，可以用 /stop 停下。")
        elif not await chat.skip():
            await say("现在没有在等结果的一步。" if chat.busy else "现在没有在做的事。")
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
                 *([f"**额度**：{q}"] if not codex and (q := await _quota(bridge.path)) else []),
                 *([f"**上下文**：{_ctx(c)}"] if not codex and (c := bridge.saved(chat.chat_id).get("ctx")) else []),
                 f"**对话**：{chat.session_id[:8] if chat.session_id else '新对话'}"]
        await say("\n".join(lines))
    elif cmd in ("/model", "/effort") and arg.lower() in ("默认", "default"):
        key = "model" if cmd == "/model" else "effort"
        chat.follow(key)
        m, e, _, _ = chat.effective()
        await say(f"好，这个聊天不再单独设置，{'模型' if key == 'model' else '思考深度'}回到默认：" +
                  (_model_name(m) if key == "model" else e) + "。")
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
