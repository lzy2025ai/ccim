"""总台（#543，实验）：一个固定的对话，用户直接说事，它判断交给哪个 Agent、新开对话还是接着已有的对话，替用户盯进度、回来汇报。

总台本身是页面专用的助手对话（platform.HELPERS 的 desk，和 Skill 助手同一套：不进对话列表、每台电脑一个），
派活、反问这两个工具在 session._desk_tools。卡片（type=work，带 desk）记在总台里，做完由总台自己汇报（新的一轮），
网页任务面板「总台」右边一栏按卡片和目标对话的状态分成 在干活 / 等你 / 今天做完。
只有电脑的主人能用；只派给这台电脑上的 Agent（别的电脑上的要跨电脑开对话、更新卡片，第一版不做）。
"""
import asyncio, time, uuid

from . import agents, registry, share

KIND = "desk"
TITLE = "总台"
ROLE = (
    "你是 ccim 平台的「总台」，用户的私人秘书。用户有事直接跟你说，你不亲自动手做，而是交给平台上合适的 AI 员工（Agent），"
    "替用户盯进度、回来汇报。\n"
    "- 用户交代一件事：先想清楚这件事跟以前的哪件有关。说得模糊（「那个」「刚才那件」「上次说的」）时，先用 overview 看全局、"
    "用 search_chats 按关键词找以前的对话，必要时 open_chat 读一读。\n"
    "- 找到正在跟这件事的对话：用 assign_work 的 chat 接着在那个对话里做（编号照抄 overview / search_chats 给的）。"
    "是新的事：用 platform_agents 看有哪些 Agent、各管什么，挑最合适的一个，用 assign_work 的 agent 新开对话交给它。\n"
    "- 判断不了是哪一件（有两三个都像）：用 ask_which 列出候选对话让用户点，不要猜；调用后这一轮只说一句「你说的是哪一件？」。"
    "用户选了以后，消息前面括号里会带上他选的对话编号。\n"
    "- 交代给 Agent 的话（text）要写清楚来龙去脉、要做什么、做完怎么算好：新对话看不到这里。title 十几个字。\n"
    "- 派出去后用一两句话告诉用户交给了谁，不用复述。对话里会自动出现一张卡片显示进度，用户能点开、改派。\n"
    "- 派出去的活做完，结果会作为一条新消息交给你：用你的口吻简短汇报结果、要用户做什么（验收、拍板），不复述全文。\n"
    "- 用户只是问进度、问现在都在忙什么：用 overview 看完直接回答，不用派活。\n"
    "- 一两句话能答的小问题直接答。不要自己改文件、跑命令去做用户交代的事，那是 AI 员工的活。\n"
    "- 回答用中文，简短，口吻像一个靠谱的秘书。")

STATES = ("working", "done", "error", "stopped", "moved", "lost")
_moved = set()                                # 改派掉的卡片：原来那一轮被停下，不再回来汇报


class Problem(Exception):
    pass


def is_desk(m):
    return (m or {}).get("helper") == KIND


def find(pf, acct=None):
    """这台电脑上的总台对话编号，还没有就是 None。"""
    for cid, m in pf.chats().items():
        if is_desk(m) and (m.get("account") or None) == acct:
            return cid
    return None


def local_agents():
    """总台能派活的 Agent：这台电脑上主人的（不含访客的、一人公司的员工）。[(编号, 名字, Agent)]"""
    return [(aid, a.get("name") or "", a) for aid, a in agents.all_agents().items()
            if not a.get("account") and not a.get("staff")]


def local_name(aid):
    return (agents.get(aid) or {}).get("name") or "Agent"


def _agent(name):
    n = (name or "").strip().lstrip("@")
    rows = local_agents()
    hit = [r for r in rows if n in (r[0], r[1])] or [r for r in rows if n and n.lower() == r[1].lower()]
    if not hit:
        raise Problem(f"这台电脑上没有「{n}」，先用 platform_agents 看看有谁（只能派给这台电脑上的 Agent）。")
    return hit[0]


def _target(pf, cid):
    """接着做的对话：编号可以是缩短的；群聊里的子对话算到外层群聊。"""
    chats = pf.chats()
    cid = (cid or "").strip()
    if cid not in chats:
        hits = [k for k in chats if len(cid) >= 6 and k.startswith(cid)]
        if len(hits) != 1:
            raise Problem("找不到这个对话，编号用 overview / search_chats 给的。")
        cid = hits[0]
    m = chats[cid]
    if m.get("mention_of") and m["mention_of"] in chats:
        cid, m = m["mention_of"], chats[m["mention_of"]]
    if share.owner_map(pf).get(cid) is not None or m.get("helper") or m.get("side_of") or m.get("project"):
        raise Problem("这个对话不能从总台接着做。")
    if not m.get("agent"):
        raise Problem("只能交给 AI 员工的对话；这个对话没有选 Agent。")
    if not agents.get(m["agent"]):
        raise Problem("这个对话的 Agent 已经删掉了。")
    return cid, m


def card_of(pf, desk, wid):
    return next((r for r in pf.channel.logs.read(desk) if r.get("type") == "work" and r.get("id") == wid), None)


def dispatch(pf, desk, text, agent=None, chat=None, title=None, depth=1, moved_from=None):
    """派一件活：agent 写名字就新开它的对话，chat 写对话编号就接着在那个对话里做。返回卡片。在主循环里调。"""
    from .session import Task
    text = (text or "").strip()
    if not text:
        raise Problem("text 要写。")
    if bool(agent) == bool(chat):
        raise Problem("agent（新开对话交给谁）和 chat（接着哪个对话）写一个。")
    if chat:
        cid, m = _target(pf, chat)
        aid = m["agent"]
        title = (pf.chat_view(cid).get("title") or "对话")
        mode = "continue"
        pf.channel._emit(cid, pf.channel.logs.append(cid, {"type": "user", "id": "u-" + uuid.uuid4().hex[:10], "text": text,
                                                           "files": [], "from_chat": desk, "relay": True, "desk": True}))
        chats = pf.chats()
        if cid in chats:
            chats[cid]["last_active"] = registry.now()
            pf.store.write(chats)
        prompt = f"[用户在总台交代了下面这件事，总台把它转到这个对话里交给你接着做；做完把结果写清楚（做了什么、怎么验收），会交回总台] {text}"
    else:
        aid, _, _ = _agent(agent)
        title = " ".join((title or text).split())[:30] or "新任务"
        cid = pf.new_chat(agent=aid, title=title)
        mode = "new"
        pf.channel._emit(cid, pf.channel.logs.append(cid, {"type": "user", "id": "u-" + uuid.uuid4().hex[:10], "text": text,
                                                           "files": [], "from_chat": desk, "dispatch": True, "desk": True}))
        prompt = f"[用户在总台交代了这件事，总台新开这个对话交给你；做完把结果写清楚（做了什么、怎么验收），会交回总台] {text}"
    task = Task(prompt)
    task.depth = depth
    start = time.time()
    pf._bridge(cid).chat(cid, "p2p").submit(task)
    wid = "k-" + uuid.uuid4().hex[:10]
    card = {"type": "work", "id": wid, "chat": cid, "agent": aid, "title": title, "state": "working",
            "desk": mode, "text": text, "start": start, **({"moved_from": moved_from} if moved_from else {})}
    pf.channel._emit(desk, pf.channel.logs.append(desk, card))
    pf.works.add(wid)
    t = asyncio.ensure_future(_wait(pf, desk, card, task, start))
    pf.background.add(t)
    t.add_done_callback(pf.background.discard)
    return card


async def _wait(pf, desk, card, task, start):
    try:
        state, answer = await task.future
    except Exception as e:
        state, answer = "error", str(e)
    pf.works.discard(card["id"])
    if card["id"] in _moved:                 # 改派掉了：原来那一轮是被停下的，不汇报
        _moved.discard(card["id"])
        return
    cid = card["chat"]
    rows = [r for r in pf.channel.logs.read(cid) if (r.get("at") or 0) >= start] if cid in pf.chats() else []
    last = next((r for r in reversed(rows) if r.get("type") == "answer"), {})
    files = [f"{r['name']}（{r['path']}）" for r in rows if r.get("type") == "file"][-10:]
    state = {"ok": "done"}.get(state, state)
    if state not in STATES:
        state = "error"
    if desk not in pf.chats():
        return
    pf.channel._emit(desk, pf.channel.logs.append(desk, dict(card, state=state, done_at=time.time())))
    how = {"done": "做完了", "error": "出错了", "stopped": "被停下了"}.get(state, "没做完")
    who = (agents.get(card["agent"]) or {}).get("name") or "Agent"
    text = answer or last.get("text") or ""
    moved = f"（这件原来交给了{local_name(card['moved_from'])}，用户在总台里改派给了{who}）" if card.get("moved_from") else ""
    pf.deliver(desk, (
        f"[你派给{who}的「{card['title']}」{how}{moved}。下面是那边最后的回答" + ("和发出的文件" if files else "") +
        "。请向用户简短汇报：结果是什么、要用户做什么（验收、拍板）；不用复述全文，用户想看细节可以点卡片进那个对话。]\n\n"
        + (text[:6000] or "（没有回答）") + ("\n\n发出的文件：\n" + "\n".join("- " + f for f in files) if files else "")))


async def reassign(pf, desk, wid, agent=None, chat=None):
    """改派：原来那件还在做就停下，卡片标成改派了，交给新的人（新开对话）或另一个对话接着做。返回新卡片。"""
    if not is_desk(pf.chats().get(desk)) or share.owner_map(pf).get(desk) is not None:
        raise Problem("找不到总台。")
    card = card_of(pf, desk, wid)
    if not card or not card.get("desk"):
        raise Problem("找不到这张卡片。")
    if card.get("state") == "moved":
        raise Problem("这件已经改派过了。")
    if chat and chat == card["chat"]:
        raise Problem("已经在这个对话里了。")
    if card.get("state") == "working" and card["id"] in pf.works:
        _moved.add(card["id"])
        try:
            await pf.stop_chat(card["chat"])
        except Exception:
            pass
    new = dispatch(pf, desk, card.get("text") or card["title"], agent=agent, chat=chat,
                   title=card["title"] if agent else None, moved_from=card["agent"])
    pf.channel._emit(desk, pf.channel.logs.append(desk, dict(card, state="moved", moved_to=new["id"], done_at=time.time())))
    pf.works.discard(card["id"])
    return new


def choices(pf, desk, question, ids):
    """反问用户说的是哪一件：在总台里记一张候选卡片，用户点了就把选的对话带上发回来。"""
    chats = pf.chats()
    items = []
    for raw in ids or []:
        try:
            cid, m = _target(pf, raw)
        except Problem:
            continue
        if cid in [x["chat"] for x in items]:
            continue
        v = pf.chat_view(cid)
        items.append({"chat": cid, "title": v.get("title") or "对话", "agent": m.get("agent"),
                      "at": m.get("last_active") or ""})
    if not items:
        raise Problem("候选对话一个都没找到，编号用 overview / search_chats 给的。")
    rec = {"type": "choices", "id": "q-" + uuid.uuid4().hex[:10], "q": (question or "你说的是哪一件？").strip()[:80],
           "items": items[:4]}
    pf.channel._emit(desk, pf.channel.logs.append(desk, rec))
    return rec


def _today(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts))) == time.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return False


def panel(pf, acct=None):
    """右边一栏「它手上的事」：{desk, working, waiting, done}。每条 {card, chat, title, agent, state, line, at}。"""
    desk = find(pf, acct)
    out = {"desk": desk, "working": [], "waiting": [], "done": []}
    if not desk:
        return out
    cards = {}
    for r in pf.channel.logs.read(desk):
        if r.get("type") == "work" and r.get("desk"):
            cards[r["id"]] = r
    chats = pf.chats()
    seen = set()
    for r in sorted(cards.values(), key=lambda r: -(r.get("done_at") or r.get("start") or r.get("at") or 0)):
        if r["chat"] in seen:
            continue                          # 同一个对话派过几次：只看最近那张
        seen.add(r["chat"])
        if r.get("state") == "moved" or r["chat"] not in chats:   # 最近那张改派走了：那边是被停下的，不算
            continue
        v = pf.chat_view(r["chat"])
        att = v.get("attention")
        row = {"card": r["id"], "chat": r["chat"], "title": v.get("title") or r.get("title"), "agent": r.get("agent"),
               "state": r.get("state"), "start": r.get("start") or r.get("at"), "done_at": r.get("done_at")}
        if att == "ask":
            out["waiting"].append(dict(row, line="等你批准"))
        elif v.get("busy") or (r.get("state") == "working" and r["id"] in pf.works):
            out["working"].append(dict(row, line=v.get("doing") or "在处理"))
        elif att in ("done", "err"):
            out["waiting"].append(dict(row, line="出错停下了，等你看看" if att == "err" else "做完了，等你验收"))
        elif _today(r.get("done_at") or 0):
            out["done"].append(dict(row, line=v.get("brief") or "做完了"))
    return out
