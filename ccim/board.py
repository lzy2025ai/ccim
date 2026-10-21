"""任务面板（#48）：哪些对话在等你、在干活、最近在跟，一句话进度，概览数据。

- 每个网页对话的记录扫一遍得到摘要（最后一个回答的时间和状态、有没有等批准的卡片、每天发了几条），按文件大小和修改时间缓存。
- 未读：对话 meta 的 seen_at（网页里打开过就更新）早于最后一个回答。平台第一次启动这个功能时，已有的对话都算看过。
  任务面板里点「知道了」记 dismissed_at：不再等你处理，但没打开过的仍算未读（#65）。
- 一句话进度：每轮回答完（网页对话，不含 @ 的子对话、项目、定时任务）用小模型总结，存在 meta 的 brief。
"""
import asyncio, datetime, logging, os, time

log = logging.getLogger("ccim.board")
_SUM = {}                                     # 对话 → (大小, 修改时间, 摘要)
BRIEF_MODEL = "haiku"
_brief_lock = None


def summary(logs, cid):
    path = logs._f(cid)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    hit = _SUM.get(cid)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    s = {"answer_at": 0, "answer_state": None, "pending": False, "days": {}, "last_user": ""}
    for r in logs.read(cid):
        t = r.get("type")
        if t == "user" and not r.get("by"):
            day = time.strftime("%Y-%m-%d", time.localtime(r.get("at") or 0))
            s["days"][day] = s["days"].get(day, 0) + 1
            s["last_user"] = r.get("text") or ""
        elif t == "answer" or (t == "mention" and r.get("state") in ("done", "error")):
            s["answer_at"], s["answer_state"] = r.get("done_at") or r.get("at") or 0, r.get("state")
        if t == "card":
            s["pending"] = bool(r.get("buttons"))
        elif t == "secret":
            s["pending"] = r.get("state") == "pending"
    _SUM[cid] = ((st.st_size, st.st_mtime), s)
    return s


def attention(meta, summ):
    """这个对话要不要你出手：ask（等批准）/ err（出错停下）/ done（回答完了没看）/ None。"""
    if not summ or meta.get("archived"):
        return None
    if summ.get("pending"):
        return "ask"
    if meta.get("busy"):
        return None
    if summ.get("answer_at", 0) > max(meta.get("seen_at") or 0, meta.get("dismissed_at") or 0):
        return "err" if summ.get("answer_state") == "error" else "done"
    return None


def unread(meta, summ):
    """回答还没打开看过（任务面板里点了「知道了」的也算，#65）。"""
    return bool(summ) and not meta.get("busy") and summ.get("answer_at", 0) > (meta.get("seen_at") or 0)


def top_level(meta):
    return not (meta.get("mention_of") or meta.get("project") or meta.get("schedule"))


def stats(pf):
    """概览：近 26 周每天发了几条、本周对话轮数、本周完成（归档）几件、各 Agent 本周轮数和最近活跃。"""
    now = datetime.date.today()
    week0 = (now - datetime.timedelta(days=now.weekday())).isoformat()
    since = (now - datetime.timedelta(days=26 * 7)).isoformat()
    heat, agents = {}, {}
    turns_week = done_week = 0
    for cid, m in pf.chats().items():
        if m.get("project") or m.get("schedule"):
            continue
        s = summary(pf.channel.logs, cid)
        aid = m.get("agent")
        for day, n in (s.get("days") or {}).items():
            if day < since:
                continue
            heat[day] = heat.get(day, 0) + n
            if day >= week0:
                if not m.get("mention_of"):
                    turns_week += n
                if aid:
                    agents.setdefault(aid, {"turns": 0, "last": ""})["turns"] += n
        if aid and m.get("last_active"):
            a = agents.setdefault(aid, {"turns": 0, "last": ""})
            a["last"] = max(a["last"], m["last_active"])
        at = m.get("archived_at")
        if m.get("archived") and at and datetime.date.fromtimestamp(at).isoformat() >= week0:
            done_week += 1
    return {"heat": heat, "turns_week": turns_week, "done_week": done_week, "agents": agents, "today": now.isoformat()}


# ----- 一句话进度 -----

PROMPT = """下面是一个对话里用户最近说的话和助手最近的回答。用一句中文概括这件事现在的进度，给用户在任务面板上扫一眼用：
在做什么、到哪一步了、下一步在等谁（等用户确认 / 等用户提供什么 / 已完成）。
不超过 40 个字，不要标点以外的符号。提到用户时用「你」（比如「等你确认」），不要写「用户」「助手」，直接写事情本身。只输出这一句。

用户最近说的：
{user}

助手最近的回答：
{answer}"""


async def make_brief(user, answer):
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
    from .session import CLI
    opts = ClaudeAgentOptions(model=BRIEF_MODEL, max_turns=1, allowed_tools=[], setting_sources=[],
                              cwd=os.path.expanduser("~"), cli_path=CLI, permission_mode="default")
    text = ""
    async for m in query(prompt=PROMPT.format(user=user[-1500:], answer=answer[-3000:]), options=opts):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    text += b.text
    return " ".join(text.split()).strip("「」\"'")[:60]


async def refresh_brief(pf, cid):
    """这一轮回答完了：更新这个对话的一句话进度。同时最多两个在跑。"""
    global _brief_lock
    if _brief_lock is None:
        _brief_lock = asyncio.Semaphore(2)
    m = pf.chats().get(cid)
    if not m or not top_level(m):
        return
    rows = pf.channel.logs.read(cid)
    ans = next((r for r in reversed(rows) if r.get("type") == "answer" and r.get("text")), None)
    if not ans:
        return
    user = next((r.get("text") or "" for r in reversed(rows) if r.get("type") == "user" and r.get("at", 0) <= ans.get("at", 0)), "")
    async with _brief_lock:
        try:
            brief = await asyncio.wait_for(make_brief(user, ans["text"]), 90)
        except Exception:
            log.warning("一句话进度没生成出来", exc_info=True)
            return
    if not brief:
        return
    chats = pf.chats()
    if cid in chats:
        chats[cid]["brief"], chats[cid]["brief_at"] = brief, time.time()
        pf.store.write(chats)
        pf.channel.events.publish({"type": "brief", "chat": cid, "brief": brief})
