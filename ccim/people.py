"""群聊里的真人（#419）：除了 Agent，对话里也能拉平台上的账号进来，人和 Agent 在同一个群里。

记在对话 meta 的 people：[{id, name, by, at, state}]，id 是账号编号（这台电脑的主人是 peers.OWNER），
state 是 in（在群里）或 invited（邀请了、对方还没同意）。建对话的人（creator，meta 的 account，没有就是主人）不在 people 里，
但总算在群里。@ 子对话、侧边对话跟着它的主对话。

- 拉人：电脑主人拉的直接进（访客的账号都是主人建的）；别人拉的要对方同意。以后平台上自己注册的账号，看的是「是不是拉人的人建的」。
- 进来的人看全部历史、能发言、发文件、@ Agent；退回、删除、改设置这些只有建群的人能做（`MEMBER_PATHS` 以外的一律拒绝）。
- 群里有两个以上真人时，Agent 只在被 @ 时回答（`quiet`）；没 @ 的话记下，下次轮到它时带上（`backlog`）。
- 谁叫的这一轮就按谁的权限跑（`fence_for` → session.Speaker）：主人不圈，访客圈在这个对话的工作目录里；
  换了人就断开、按新的权限接着原会话；别人的话不补进正在跑的这一轮，排到后面。
- 各人看到哪记在 meta 的 seen_of（建群的人还用 seen_at），被 @ 的记在 pinged，用来算「等你处理」。
"""
import os, pwd, time

from .peers import OWNER

FENCE = "@people:"                            # 主人建的群里有访客时，会话按这个占位账号圈起来（share.fence 认不出它的文件夹，只留工作目录）

# 不是建群的人能调的接口（其余的一律只有建群的人能做）
MEMBER_GET = {"/api/chat", "/api/chat/sig", "/api/doc", "/api/file", "/api/file/part", "/api/ws/ls", "/api/chat/people"}
MEMBER_POST = {"/api/chat/send", "/api/chat/stop", "/api/chat/skip", "/api/chat/mention", "/api/chat/mention/stop",
               "/api/chat/seen", "/api/chat/action", "/api/chat/people", "/api/chat/join"}
INVITED = {"/api/chat", "/api/chat/sig", "/api/chat/join", "/api/chat/people", "/api/chat/seen"}


class Denied(Exception):
    pass


def me(acct):
    """请求的人：访客是账号编号，这台电脑的主人是 OWNER。"""
    return acct or OWNER


def root(pf, cid, chats=None):
    """@ 子对话、侧边对话跟着它们的主对话。"""
    chats = pf.chats() if chats is None else chats
    for _ in range(3):
        m = chats.get(cid) or {}
        up = m.get("mention_of") or m.get("side_of")
        if not up:
            break
        cid = up
    return cid


def creator(pf, cid):
    from . import share
    return share.chat_owner(pf, root(pf, cid)) or OWNER


def people(pf, cid, chats=None):
    """被拉进来的人（不含建群的人），含还没同意的。"""
    chats = pf.chats() if chats is None else chats
    m = chats.get(root(pf, cid, chats)) or {}
    return [p for p in m.get("people") or [] if isinstance(p, dict) and p.get("id")]


def humans(pf, cid):
    """在群里的真人：建群的人 + 同意了的。"""
    return [creator(pf, cid)] + [p["id"] for p in people(pf, cid) if p.get("state") == "in"]


def state_of(pf, cid, who):
    """这个人和这个对话的关系：creator / in / invited / None。"""
    if creator(pf, cid) == who:
        return "creator"
    for p in people(pf, cid):
        if p["id"] == who:
            return p.get("state") or "in"
    return None


def quiet(pf, cid):
    return len(humans(pf, cid)) >= 2


def owner_name():
    """这台电脑主人的名字：系统用户的全名，取不到写「主人」。"""
    try:
        n = (pwd.getpwuid(os.getuid()).pw_gecos or "").split(",")[0].strip()
    except Exception:
        n = ""
    return n or "主人"


def name_of(who):
    from . import share
    if who == OWNER:
        return owner_name()
    return share.guest(who).get("name") or "访客"


def fence_for(pf, cid, who):
    """这一轮按谁的权限跑（session.Speaker 的 fence）：电脑主人不圈（""）；建群的访客按它本来的（None）；
    别的访客圈在这个对话的工作目录里（占位账号，share.fence 认不出它的文件夹）。"""
    if who == OWNER:
        return ""
    if who == creator(pf, cid):
        return None
    return FENCE + root(pf, cid)


_vis = {"at": 0, "ver": None, "map": {}}


def visible_map(pf):
    """主对话编号 → 被拉进来的人（含还没同意的）。推送、列表都要用，和 share.owner_map 一样缓存几秒。"""
    ver = getattr(pf.store, "ver", 0)
    if time.time() - _vis["at"] > 5 or _vis["ver"] != ver:
        out = {}
        for cid, m in pf.chats().items():
            ps = [p["id"] for p in m.get("people") or [] if isinstance(p, dict) and p.get("id")]
            if ps:
                out[cid] = set(ps)
        _vis.update(at=time.time(), ver=ver, map=out)
    return _vis["map"]


def candidates(pf, cid, who):
    """能拉进来的人：这台电脑的主人和来过这台电脑的访客，去掉已经在的。"""
    from . import registry
    have = {creator(pf, cid)} | {p["id"] for p in people(pf, cid)}
    out = [] if OWNER in have else [{"id": OWNER, "name": owner_name()}]
    guests = registry.read_state(pf.home).get("guests") or {}
    out += [{"id": a, "name": g.get("name") or a} for a, g in guests.items() if a not in have]
    return out


def view(pf, cid, who):
    """网页用：我是谁、什么身份，群里的人，能拉谁，群里的 Agent 长什么样（被拉进来的人的 Agent 列表里没有它们）。"""
    from . import agents, groups
    st = state_of(pf, cid, who)
    c = creator(pf, cid)
    m = pf.chats().get(root(pf, cid)) or {}
    look = []
    for aid in [m.get("agent")] + groups.members(m):
        a = agents.get(aid or "")
        if a:
            look.append({k: a.get(k) for k in ("id", "name", "seal", "color", "avatar", "base")})
    return {"me": who, "role": st, "creator": {"id": c, "name": name_of(c)},
            "people": [dict(p, name=name_of(p["id"])) for p in people(pf, cid)],
            "candidates": candidates(pf, cid, who) if st in ("creator", "in") else [],
            "quiet": quiet(pf, cid), "agents": look}


def _write(pf, cid, ps):
    chats = pf.chats()
    rid = root(pf, cid)
    if rid not in chats:
        raise Denied("找不到这个对话。")
    chats[rid]["people"] = ps
    pf.store.write(chats)


def add(pf, cid, who, ids):
    """拉人。主人拉的直接进；别人拉的先发邀请，对方同意才进。返回新的 view。"""
    if state_of(pf, cid, who) not in ("creator", "in"):
        raise Denied("你不在这个群里。")
    if pf.chats().get(root(pf, cid), {}).get("helper") or pf.chats().get(root(pf, cid), {}).get("pocket"):
        raise Denied("这个对话不能拉人。")
    ok = {c["id"] for c in candidates(pf, cid, who)}
    ps = people(pf, cid)
    for i in dict.fromkeys(ids or []):
        if i not in ok:
            continue
        ps.append({"id": i, "by": who, "at": time.time(), "state": "in" if who == OWNER else "invited"})
    _write(pf, cid, ps)
    return view(pf, cid, who)


def remove(pf, cid, who, ids):
    """移出群：建群的人能移任何人，别人只能退自己。"""
    st = state_of(pf, cid, who)
    ids = set(ids or [])
    if st != "creator" and ids - {who}:
        raise Denied("只有建群的人能把别人移出去。")
    _write(pf, cid, [p for p in people(pf, cid) if p["id"] not in ids])
    return view(pf, cid, who)


def join(pf, cid, who, accept):
    ps = people(pf, cid)
    hit = next((p for p in ps if p["id"] == who), None)
    if not hit:
        raise Denied("邀请已经撤回了。")
    if accept:
        hit.update(state="in", joined=time.time())
    else:
        ps.remove(hit)
    _write(pf, cid, ps)
    return view(pf, cid, who) if accept else {"left": True}


def label(pf, cid, who):
    """记在用户消息上的发送人；群里只有一个人时不记。"""
    if len(humans(pf, cid)) < 2 and who == creator(pf, cid):
        return None
    return {"id": who, "name": name_of(who)}


def note(pf, cid):
    """交给 Agent 的话前面带上：群里有哪些人、谁建的群。"""
    hs = humans(pf, cid)
    if len(hs) < 2:
        return ""
    c = creator(pf, cid)
    names = "、".join(name_of(h) + ("（建群的人）" if h == c else "") for h in hs)
    return (f"（这是多人群聊，群里的人：{names}。每条消息开头的【名字】是谁说的。别人只在 @ 你时才会交给你，"
            f"他们之间没 @ 你的对话会在下次 @ 你时一起带上）\n")


def ping(pf, cid, ids):
    """@ 了群里的人：记下时间，算「等你处理」。"""
    if not ids:
        return
    chats = pf.chats()
    rid = root(pf, cid)
    if rid not in chats:
        return
    inside = set(humans(pf, cid))
    pinged = dict(chats[rid].get("pinged") or {})
    for i in ids:
        if i in inside:
            pinged[i] = time.time()
    chats[rid]["pinged"] = pinged
    pf.store.write(chats)


def seen(pf, cid, who):
    chats = pf.chats()
    if cid not in chats:
        return
    so = dict(chats[cid].get("seen_of") or {})
    so[who] = time.time()
    chats[cid]["seen_of"] = so
    if who == creator(pf, cid):
        chats[cid]["seen_at"] = time.time()
    pf.store.write(chats)


def backlog(pf, cid):
    """多人群里没 @ Agent 的话（记录带 quiet、还没交给它的）：拼成一段放在下次交给它的消息前面，并标成已交。"""
    rows = [r for r in pf.channel.logs.read(cid) if r.get("type") == "user" and r.get("quiet") and not r.get("handed")]
    if not rows:
        return ""
    chats = pf.chats()
    upto = (chats.get(cid) or {}).get("quiet_upto", 0)
    rows = [r for r in rows if r.get("at", 0) > upto]
    if not rows:
        return ""
    if cid in chats:
        chats[cid]["quiet_upto"] = rows[-1].get("at", time.time())
        pf.store.write(chats)
    lines = [f"【{(r.get('person') or {}).get('name') or '用户'}】{(r.get('text') or '').strip()}" for r in rows]
    return "（群里在你上次回答之后的对话，没 @ 你，供参考）\n" + "\n".join(lines) + "\n\n——\n\n"


def attention(pf, cid, who, c):
    """对话列表里这个人的「等你处理」：被 @ 了没看；不是建群的人，有新消息没看也算。改 c（chat_view 的副本）。"""
    rid = root(pf, cid)
    m = pf.chats().get(rid) or {}
    st = state_of(pf, rid, who)
    if st == "invited":
        c["people_role"] = st
        p = next((p for p in people(pf, rid) if p["id"] == who), {})
        c["invite"] = {"by": name_of(p.get("by") or OWNER)}
        c["attention"] = "ask"
        return c
    c["people_role"] = st
    seen_t = (m.get("seen_of") or {}).get(who, 0)
    if st == "creator":
        seen_t = max(seen_t, m.get("seen_at", 0))
    if (m.get("pinged") or {}).get(who, 0) > seen_t:
        c["attention"], c["at_me"] = "ask", True
    elif st == "in":
        try:
            la = time.mktime(time.strptime(c.get("last_active") or "", "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            la = 0
        c["attention"] = "done" if la and la > seen_t else None
    return c
