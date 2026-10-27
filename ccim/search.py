"""Agent 搜自己参与过的对话（#180）：search_chats / open_chat 两个工具背后的东西。

范围是这个 Agent 的：网页里它的对话（私聊、它当主 Agent 的群聊、它是成员的群聊、定时任务），
别的电脑上的群聊里它被 @ 的子对话，飞书、微信里的对话，工作目录下终端、桌面端的 Claude Code 对话。
访客（#154）只有它自己账号的网页对话；飞书、微信、终端的对话属于电脑主人，不给。
助理（没选 Agent 的对话、右下角的助理，#294）：aid 是 None，范围是所有助理对话（也是它当主角的群聊），没有飞书、微信、终端。
第一版只按关键词找（几个词都要出现在同一条消息里），不建索引。
"""
import os, time

from . import agents, handoff, history, share
from .groups import members

TERMINAL = 100                                # 终端、桌面端的对话最多看最近这么多个
HITS = 15                                     # 最多给这么多条结果
PER_CHAT = 3                                  # 每个对话最多几条
SNIP = 100                                    # 关键词前后各留这么多字
ME = object()                                 # sources 里「说话的是这个 Agent 自己」
_CACHE = {}                                   # (底座, 会话) → ((大小, 修改时间), 记录)：别处的对话记录，不占 history 的缓存


def _ext_log(base, sid, cwd):
    path = history.codex_file(sid) if base == "codex" else handoff.transcript_path(sid, cwd)
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    sig = (st.st_size, st.st_mtime) if st else None
    hit = _CACHE.get((base, sid))
    if sig and hit and hit[0] == sig:
        return hit[1]
    rows = history.codex(sid, limit=100000) if base == "codex" else history.claude(sid, cwd, limit=100000)
    if sig:
        if len(_CACHE) > 500:
            _CACHE.clear()
        _CACHE[(base, sid)] = (sig, rows)
    return rows


def sources(pf, aid, acct):
    """{编号: (标题, 在哪, 读记录的函数, 说话的人, 补充说明, 最近时间)}。编号：网页对话的编号，或 x:<会话>。"""
    a = agents.get(aid) or {}
    owners = share.owner_map(pf)
    chats = pf.chats()
    out = {}
    pick = []
    for cid, m in chats.items():
        if owners.get(cid) != acct or m.get("helper") or m.get("side_of"):
            continue
        outer = m.get("mention_of")
        if m.get("agent") == aid and outer and outer in chats:
            pick.append(outer)                # 本机对话里被 @ 的子对话：内容在外层对话里都有（外层可能没选 Agent）
        elif m.get("agent") == aid or aid in members(m):
            pick.append(cid)
    name = lambda x: pf.mentions._agent_name(x)
    for cid in dict.fromkeys(pick):
        m = chats[cid]
        outer = m.get("mention_of")
        ms = members(m)
        where = "群聊" if outer or ms else "定时任务" if m.get("schedule") else "网页对话"
        title = m.get("title") or pf.chat_view(cid).get("title") or "对话"
        owner_name = ME if m.get("agent") == aid else pf.mentions._name(m, {})
        if outer:                             # 别的电脑上的群聊，这里只有它被 @ 的那部分
            note = "别的电脑上的群聊，你在里面被 @ 过"
        elif ms:
            others = [name(x) for x in ms if x != aid]
            note = ("你是主 Agent" if m.get("agent") == aid else f"主 Agent 是{owner_name}，你是成员") + \
                (f"，其他成员：{'、'.join(others)}" if others else "")
        elif m.get("agent") != aid:
            note = f"{owner_name}的对话，你在里面被 @ 过"
        else:
            note = ""
        out[cid] = (title, where, (lambda cid=cid: pf.channel.logs.read(cid)), owner_name, note, m.get("last_active") or "")
    if not aid or share.of_agent(a) != acct:  # 飞书、微信、终端里的对话是电脑主人的；助理没有
        return out
    cwd = agents.cwd(a)
    for r in pf.external_chats(aid):          # 飞书、微信（网页里接着聊过的已经去掉）
        out[r["id"]] = (r["title"], r["source"], (lambda r=r: _ext_log(r["base"], r["sid"], cwd)), ME, "", r.get("last_active") or "")
    if (a.get("base") or "claude") == "claude":
        mine = {m.get("session_id") for m in chats.values()} | {k[2:] for k in out if k.startswith("x:")}
        try:
            from claude_agent_sdk import list_sessions
            sessions = list_sessions(directory=cwd, limit=TERMINAL)
        except Exception:
            sessions = []
        for x in sessions:
            if x.session_id in mine:
                continue
            title = " ".join((x.custom_title or x.summary or x.first_prompt or "终端里的对话").split())[:30]
            out["x:" + x.session_id] = (title, "终端或桌面端", (lambda sid=x.session_id: _ext_log("claude", sid, cwd)), ME, "",
                                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x.last_modified / 1000)))
    return out


def _speaker(r, owner, pf, aid):
    """谁说的。是这个 Agent 自己说的写「你」：它不一定记得自己叫什么，写名字会被当成别人。"""
    if r.get("type") == "user":
        who = r.get("by") if r.get("by") and r["by"] != "assistant" else None
    elif r.get("type") == "mention":
        who = r.get("agent")
    else:
        return "你" if owner is ME else owner
    return "你" if who and who == aid else pf.mentions._agent_name(who) if who else "用户"


def _me(aid):
    return "Agent「%s」" % ((agents.get(aid) or {}).get("name") or "") if aid else "助理"


def _when(at):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError):
        return str(at or "")[:16]


def _usable(r):
    return (r.get("type") in ("user", "answer") or (r.get("type") == "mention" and r.get("state") == "done")) \
        and (r.get("text") or "").strip()


ORDER = ("群聊", "网页对话", "定时任务")      # 列出来时的顺序，别的（飞书、微信、终端）排在后面
SHOW = 20                                     # 每类最多列这么多个


def _ts(v):
    v = str(v or "")
    return v[:16] if v[:2] == "20" else _when(v) if v else ""


def listing(src, aid, skip=None):
    """不写关键词：列出这个 Agent 参与的所有对话，按类分组、最近的在前。"""
    if not src:
        return "你还没有参与过任何对话。"
    groups = {}
    for cid, (title, where, _, _, note, last) in src.items():
        groups.setdefault(where, []).append((str(last), cid, title, note))
    kinds = sorted(groups, key=lambda w: (ORDER.index(w) if w in ORDER else len(ORDER), w))
    lines = [f"你（{_me(aid)}）一共参与了 {len(src)} 个对话：" + "、".join(f"{w} {len(groups[w])} 个" for w in kinds) +
             "。要看内容用 open_chat，id 照抄：", ""]
    for w in kinds:
        rows = sorted(groups[w], reverse=True)
        lines.append(f"## {w}")
        for last, cid, title, note in rows[:SHOW]:
            lines.append(f"- 「{title}」{'（就是现在这个对话）' if cid == skip else ''}" + (f" · {note}" if note else "") +
                         (f" · 最近 {_ts(last)}" if last else "") + f"\n  open_chat id={short(cid)}")
        if len(rows) > SHOW:
            lines.append(f"- 还有 {len(rows) - SHOW} 个更早的没列出来，可以用关键词搜")
        lines.append("")
    return "\n".join(lines).strip()


def search(pf, aid, acct, query, skip=None):
    """返回给助手看的文字。skip：当前这个对话（不用搜自己正在说的）。"""
    words = [w.lower() for w in (query or "").split() if w.strip()]
    src = sources(pf, aid, acct)
    if not words:
        return listing(src, aid, skip)
    hits = []
    for cid, (title, where, load, owner, _, _) in src.items():
        if cid == skip:
            continue
        try:
            rows = load()
        except Exception:
            continue
        n = 0
        for i in range(len(rows) - 1, -1, -1):
            r = rows[i]
            if not _usable(r):
                continue
            low = r["text"].lower()
            if all(w in low for w in words):
                k = low.find(words[0])
                t = r["text"]
                snip = ("…" if k > SNIP else "") + " ".join(t[max(0, k - SNIP):k + SNIP].split()) + ("…" if k + SNIP < len(t) else "")
                hits.append((r.get("at") or 0, cid, i, title, where, _speaker(r, owner, pf, aid), snip))
                n += 1
                if n >= PER_CHAT:
                    break
    if not hits:
        return f"在你参与过的 {len(src)} 个对话里没找到同时包含「{' '.join(words)}」的消息。可以换个说法、少写几个词再试。"
    hits.sort(key=lambda h: float(h[0]) if isinstance(h[0], (int, float)) else 0, reverse=True)
    lines = [f"找到 {len(hits)} 条（最近的在前{'，只列前 %d 条' % HITS if len(hits) > HITS else ''}）。"
             f"标「你」的是你自己（{_me(aid)}）说的。要看前后文用 open_chat（不是 read_chat），id、at 照抄：", ""]
    for at, cid, i, title, where, who, snip in hits[:HITS]:
        lines.append(f"- {where}「{title}」· {_when(at)} · {who}：{snip}\n  open_chat id={short(cid)} at={i}")
    return "\n".join(lines)


def short(cid):
    return cid[:10] if cid.startswith("x:") else cid


def open_chat(pf, aid, acct, cid, around=None, n=10):
    src = sources(pf, aid, acct)
    if cid not in src:                        # 结果里的编号是缩短的；也认 list_chats 那种不带 x: 的会话编号
        key = cid[2:] if cid.startswith("x:") else cid
        hits = [k for k in src if len(key) >= 8 and (k.startswith("x:" + key) or k == key)]
        if len(hits) != 1:
            return None
        cid = hits[0]
    title, where, load, owner = src[cid][:4]
    rows = [(i, r) for i, r in enumerate(load()) if _usable(r)]
    if around is None:
        part = rows[-n:]
    else:
        k = next((j for j, (i, _) in enumerate(rows) if i >= around), len(rows) - 1)
        part = rows[max(0, k - n // 2):k + n // 2 + 1]
    body = []
    for i, r in part:
        t = r["text"]
        if len(t) > 4000:
            t = t[:4000] + "…（后面省略）"
        body.append(f"[{i}] {_speaker(r, owner, pf, aid)}（{_when(r.get('at'))}）：{t}")
    head = (f"{where}「{title}」，共 {len(rows)} 条，下面是第 {part[0][0] if part else 0}～{part[-1][0] if part else 0} 条附近"
            f"（标「你」的是你自己，{_me(aid)}）：\n\n")
    return head + "\n\n".join(body)
