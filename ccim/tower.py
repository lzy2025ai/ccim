"""塔台（#438）：今天每个 Agent 几点在干活、几点在等你，排好的定时任务。

只用记下来的数据，不估算：
- 网页对话（含 Agent 的对话、群里被 @ 的子对话、定时任务的对话）：记录里每条回答有写下的时刻 at 和这一轮用时 elapsed，
  一轮 = [at - elapsed, at]；审批卡片（card）、输密码卡片（secret）每次更新都追加一行，第一次出现到第一次不再有按钮就是等你的时间。
  Logs.read 会把同一张卡片合并成一条、只留第一次的时间，所以这里自己读原始行。
- 正在进行的一轮：开始时刻是渠道记的 turn_t0（没有就用 meta 的 since）。
- 飞书、微信：它们的进程只在状态里记着正在进行那一轮的 since 和步骤，结束过的轮次没有带时间的记录，所以只画正在进行的。
- 「等你」只算等批准、等输密码。回答完最后一句在问用户，判断不可靠，第一版不算。
"""
import datetime, json, os, time

from . import agents, registry, share
from .schedules import next_time

_SCAN = {}                                    # 记录文件 → ((大小, 修改时间), 扫出来的)
MAX_SCHED = 24                                # 每个定时任务今天最多画几次（每隔几分钟的只画下一次）


def _ts(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _live(r):
    if r.get("type") == "secret":
        return r.get("state") == "pending"
    return bool(r.get("buttons")) and (not r.get("color") or r.get("color") == "orange")


def scan(path):
    """一个网页对话的记录 → {turns: [[开始, 结束, 状态, 回答开头]], cards: {id: [出现, 处理完或 None, 最后一行]}, users: [时刻]}。"""
    try:
        st = os.stat(path)
    except OSError:
        return {"turns": [], "cards": {}, "users": []}
    hit = _SCAN.get(path)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    turns, cards, users = [], {}, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t, at = r.get("type"), r.get("at")
                if not at:
                    continue
                if t == "answer":
                    end = r.get("done_at") or at
                    text = (r.get("text") or "").strip()
                    turns.append([end - float(r.get("elapsed") or 0), end, r.get("state") or "done",
                                  text.splitlines()[0][:120] if text else ""])
                elif t == "user":
                    users.append(at)
                elif t in ("card", "secret") and r.get("id"):
                    c = cards.get(r["id"])
                    if c is None:
                        cards[r["id"]] = c = [at, None, r]
                    if not _live(r) and c[1] is None:
                        c[1] = at
                    c[2] = r
    except OSError:
        pass
    out = {"turns": turns, "cards": cards, "users": users}
    _SCAN[path] = ((st.st_size, st.st_mtime), out)
    return out


def _clip(spans, lo, hi):
    return [[max(a, lo), min(b, hi)] + rest for a, b, *rest in spans if b > lo and a < hi and min(b, hi) > max(a, lo)]


def _union(spans):
    tot, end = 0.0, None
    for a, b, *_ in sorted(spans):
        if end is None or a > end:
            tot += b - a
            end = b
        elif b > end:
            tot += b - end
            end = b
    return tot


def _card_view(cid, rid, r):
    """待处理的卡片：手机卡片上直接允许 / 拒绝要用到按钮。"""
    if r.get("type") == "secret":
        return {"chat": cid, "id": rid, "secret": True, "title": r.get("title") or "需要输入密码"}
    title = (r.get("title") or "").split(" · ")[0]
    return {"chat": cid, "id": rid, "title": title, "body": r.get("body") or "", "buttons": r.get("buttons") or []}


def today(pf):
    """这台电脑今天的塔台数据（主人看的，访客的对话不算）。"""
    now = time.time()
    d0 = datetime.datetime.combine(datetime.date.today(), datetime.time())
    day0, day1 = d0.timestamp(), (d0 + datetime.timedelta(days=1)).timestamp()
    day = d0.strftime("%Y-%m-%d")
    chats = pf.chats()
    owners = share.owner_map(pf)
    ch = pf.channel
    rows = {}                                 # Agent 编号（临时对话是 ""）→ 行

    def row(aid):
        if aid not in rows:
            rows[aid] = {"agent": aid or None, "spans": [], "waits": [], "chats": [], "ext": []}
        return rows[aid]

    for cid, m in chats.items():
        if owners.get(cid) or m.get("helper") or m.get("pocket") and not m.get("agent"):
            continue
        busy = bool(m.get("busy"))
        if not busy and (m.get("last_active") or "") < day:
            continue
        s = scan(ch.logs._f(cid))
        turns = [list(t) for t in s["turns"]]
        start = None
        if busy:
            start = ch.turn_t0.get(cid) or _ts(m.get("since")) or now
            turns.append([start, now, "work", ""])
        waits = []
        pending = []
        for rid, (a, b, r) in s["cards"].items():
            if b is None and _live(r):
                if busy and a >= (start or 0) - 1:
                    b = now
                    pending.append((a, rid, r))
                else:                         # 没处理就结束了的（平台重启、那一轮停了）：算到那一轮结束
                    b = next((t[1] for t in turns if t[1] >= a), a)
            if b is not None and b > a:
                waits.append([a, b])
        turns, waits = _clip(turns, day0, day1), _clip(waits, day0, day1)
        if not turns and not waits and not busy:
            continue
        rw = row(m.get("agent") or "")
        rw["spans"] += [t[:3] + [m.get("title") or ""] for t in turns]      # [开始, 结束, 状态, 对话标题]
        rw["waits"] += waits
        last = max([t[1] for t in turns] + [w[1] for w in waits] + [0])
        info = {"id": cid, "title": m.get("title") or "对话", "busy": busy, "last": now if busy else last,
                "brief": m.get("brief") or "", "steps": (ch.progress_of.get(cid) or {}).get("steps") or [],
                "archived": bool(m.get("archived"))}
        if pending:
            a, rid, r = min(pending, key=lambda x: x[0])
            info["card"], info["wait_since"] = _card_view(cid, rid, r), a
        done = [t for t in turns if t[2] != "work"]
        if done and not busy:
            info["end_state"], info["end_text"] = done[-1][2], done[-1][3]
        rw["chats"].append(info)

    for aid in agents.all_agents():           # 飞书、微信里正在进行的那一轮
        for kind, proc, path in agents.linked_paths(aid):
            st = (registry.read_state(proc).get("chats") or {})
            got = (st.get(path) or {}).values() if kind == "weixin" else st.values()
            for c in got:
                if not c.get("busy"):
                    continue
                a = _ts(c.get("since")) or now
                rw = row(aid)
                rw["spans"] += _clip([[a, now, "work", c.get("asked") or ""]], day0, day1)
                rw["ext"].append({"source": "微信" if kind == "weixin" else "飞书", "since": a,
                                  "steps": c.get("steps") or [], "asked": c.get("asked") or ""})

    out = []
    for aid, rw in rows.items():
        cs = sorted(rw["chats"], key=lambda c: c["last"], reverse=True)
        waiting = sorted([c for c in cs if c.get("card")], key=lambda c: c["wait_since"])
        working = [c for c in cs if c["busy"]]
        focus = (waiting or working or cs or [None])[0]
        if waiting:
            state = "wait"
        elif working or rw["ext"]:
            state = "work"
        elif focus and focus.get("end_state") == "error" and not focus["archived"]:
            state = "err"
        elif rw["spans"]:
            state = "done"
        else:
            state = "idle"
        out.append({"agent": rw["agent"], "state": state, "spans": sorted(rw["spans"]), "waits": sorted(rw["waits"]),
                    "waited": round(_union(_clip(rw["waits"], day0, now))), "focus": focus, "ext": rw["ext"],
                    "waiting": [c["card"] for c in waiting], "work_n": len(working) + len(rw["ext"]),
                    "last": max([c["last"] for c in cs] + [e["since"] for e in rw["ext"]] + [0])})

    return {"day": day, "now": now, "day0": day0, "day1": day1, "rows": out, "schedules": _schedules(pf, chats, now, day1),
            "agents": [aid for aid, a in agents.all_agents().items() if not a.get("account")]}


def _sched_agents(s, chats):
    t = s.get("target") or {}
    if t.get("kind") == "agent":
        return [t.get("id")]
    if t.get("kind") == "chat":
        return [(chats.get(t.get("id")) or {}).get("agent") or ""]
    if t.get("kind") == "group":
        return t.get("agents") or [(chats.get(t.get("id")) or {}).get("agent") or ""]
    return t.get("agents") or []


def _schedules(pf, chats, now, day1):
    """今天还没跑的定时任务：[{id, name, at, agents, every}]。"""
    out = []
    for sid, s in (pf.schedules.all() or {}).items():
        if not s.get("on", True) or not s.get("next_run") or s.get("account"):
            continue
        at, n = s["next_run"], 0
        while at and at < day1 and n < MAX_SCHED:
            if at >= now - 60:
                out.append({"id": sid, "name": s.get("name") or "定时任务", "at": at, "agents": _sched_agents(s, chats),
                            "every": s.get("every") if s.get("freq") == "every" else None})
                n += 1
                if s.get("freq") == "every":
                    break
            at = next_time(s, at)
    return sorted(out, key=lambda x: x["at"])
