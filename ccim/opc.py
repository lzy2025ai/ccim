"""一人公司（#470，取代 #454 把 Agent 分到公司里的做法）：侧栏里独立的入口，像公司 IM。

公司只有一个，存在这台电脑平台 state.json 的 opc：{name, rules, group, members, created}。
members = {成员编号: {kind, title 岗位, boss 直属上级（成员编号，空 = 老板）, created, …}}，人和 AI 都是成员：
- kind=ai：员工。编号就是它背后的 Agent 编号（agents.json 里带 staff 的，不出现在 AI 员工页），chat 是它唯一的固定对话窗口。
  干活的地方是它自己的工作区（agents.workspace），也就是它的档案：记忆.md 由它自己写，CLAUDE.md 把记忆带进每次会话。
- kind=human：平台上的账号（account），进公司就进大群。

公司大群是不挂主 Agent 的对话（meta company），@ 谁谁回答，不 @ 只记下（Platform.send）。
员工的系统提示后面接 `note_for`：公司、岗位、上下级、同事、规章、怎么用档案。只在主循环里改。
"""
import os
import time

from . import agents, registry

GROUP = "opc"                                 # 大群 meta 的 company 值
MEMORY = "记忆.md"
CLAUDE_MD = "# 我的档案\n\n这个文件夹是你自己的档案。下面是你的长期记忆，由你自己维护。\n\n@记忆.md\n"


class Problem(Exception):
    pass


def get(pf):
    return registry.read_state(pf.home).get("opc") or {}


def _write(pf, data):
    registry.write_state(pf.home, opc=data)


def is_staff(aid):
    return bool((agents.get(aid or "") or {}).get("staff"))


def _name(pf, mid, m):
    if m.get("kind") == "human":
        from . import people
        return people.name_of(m.get("account"))
    return (agents.get(mid) or {}).get("name") or "已删除"


def view(pf):
    """给网页：登录的这个人、公司和成员（按进来的先后）。还没加过人时公司是空的（没名字、没大群），不用先建。"""
    from . import people
    c = get(pf)
    chats = pf.chats()
    g = chats.get(c.get("group") or "") or {}
    live = lambda cid: cid if cid in chats and not chats[cid].get("archived") else None
    ms = [{"id": mid, "kind": m.get("kind") or "ai", "name": _name(pf, mid, m), "title": m.get("title") or "",
           "boss": m.get("boss") or "", "chat": live(m.get("chat")), "account": m.get("account")}
          for mid, m in sorted((c.get("members") or {}).items(), key=lambda x: x[1].get("created") or "")]
    return {"name": c.get("name") or "", "rules": c.get("rules") or "", "group": live(c.get("group")),
            "group_active": g.get("last_active"), "members": ms,
            "me": people.owner_name()}


# ---------- 员工的系统提示 ----------

def note_for(pf, aid):
    """接在员工角色说明后面：它在公司里是谁、和谁共事、规章、档案怎么用。不是员工返回空。"""
    c = get(pf)
    ms = c.get("members") or {}
    me = ms.get(aid)
    if not me or me.get("kind") == "human":
        return ""
    name = lambda mid: _name(pf, mid, ms[mid])
    tag = lambda mid: name(mid) + (f"（{ms[mid]['title']}）" if ms[mid].get("title") else "") + ("，真人" if ms[mid].get("kind") == "human" else "")
    boss = me.get("boss") if me.get("boss") in ms else ""
    subs = [mid for mid, m in ms.items() if m.get("boss") == aid]
    others = [mid for mid in ms if mid != aid and mid != boss and mid not in subs]
    lines = [f"\n\n## 你是「{c.get('name') or '公司'}」的员工",
             f"岗位：{me.get('title') or '还没定'}。",
             f"直属上级：{tag(boss) if boss else '老板（和你对话的人，公司是他一个人的）'}。"]
    if subs:
        lines.append("直属下级：" + "、".join(tag(s) for s in subs) + "。")
    if others:
        lines.append("其他同事：" + "、".join(tag(s) for s in others) + "。")
    lines += ["",
              "你和老板只有这一个对话窗口，会一直聊下去，不会另开新对话；公司大群里有人 @ 你时，你在群里回答。",
              "要同事配合就用 ask_agent 找他；要动某个项目的代码、资料，先用 platform_agents 看有哪些项目 Agent，再用 ask_agent 交给对应的那个。",
              "",
              "### 你的档案",
              f"你的工作目录就是你的档案，只属于你。里面的「{MEMORY}」是你的长期记忆，每次开工都会带上。",
              "老板交代的规矩、偏好、正在跟的事，大群里宣布的事，值得以后还记得的就自己写进去（一条一行，前面写日期）；过时的自己删改。记不记、怎么记由你判断，老板会翻看。"]
    if (c.get("rules") or "").strip():
        lines += ["", "### 公司规章", c["rules"].strip()]
    return "\n".join(lines)


def _relaunch(pf, only=None):
    """公司、成员变了：员工下一条消息时带上新的说明。"""
    for mid, m in (get(pf).get("members") or {}).items():
        if m.get("kind") != "human" and (only is None or mid in only):
            pf.relaunch_agent(mid)


# ---------- 公司 ----------

def _group(pf, c):
    """公司大群：没有（或者被标成已完成、删了）就建一个。"""
    gid = c.get("group")
    chats = pf.chats()
    if gid and gid in chats and not chats[gid].get("archived"):
        return gid
    gid = pf.new_chat(title=f"{c['name']} 大群")
    chats = pf.chats()
    chats[gid].update(company=GROUP, title_auto=False)
    pf.store.write(chats)
    c["group"] = gid
    return gid


def _sync_group(pf, c):
    """大群的成员跟着公司走：员工是群成员，真人是被拉进来的人。"""
    from . import groups, people
    gid = c.get("group")
    chats = pf.chats()
    if not gid or gid not in chats:
        return
    ms = c.get("members") or {}
    want = [mid for mid, m in ms.items() if m.get("kind") != "human" and agents.get(mid)]
    have = groups.members(chats[gid])
    add, drop = [a for a in want if a not in have], [a for a in have if a not in want]
    if add or drop:
        groups.set_members(pf, gid, add=add, remove=drop)
    folks = [m["account"] for m in ms.values() if m.get("kind") == "human" and m.get("account")]
    now = [p["id"] for p in people.people(pf, gid)]
    if [a for a in folks if a not in now]:
        people.add(pf, gid, people.OWNER, [a for a in folks if a not in now])
    if [a for a in now if a not in folks]:
        people.remove(pf, gid, people.OWNER, [a for a in now if a not in folks])
    title = f"{c['name']} 大群"
    chats = pf.chats()
    if chats[gid].get("title") != title:
        chats[gid]["title"] = title
        pf.store.write(chats)


def _default_name():
    from . import people
    return f"{people.owner_name()}的公司"[:20]


def save(pf, fields):
    """改公司名字、规章；还没有公司就顺手建上（名字没给用「谁的公司」）。"""
    c = get(pf)
    new = not c
    if "name" in fields or new:
        name = " ".join(str(fields.get("name") or "").split())[:20]
        if not name and not new:
            raise Problem("给公司起个名字。")
        c["name"] = name or _default_name()
    if "rules" in fields:
        c["rules"] = str(fields.get("rules") or "").strip()[:8000]
    if new:
        c.update(members={}, created=registry.now())
    _group(pf, c)
    _write(pf, c)
    _sync_group(pf, c)
    _relaunch(pf)
    return {"group": c["group"]}


# ---------- 成员 ----------

def _boss_ok(ms, mid, boss):
    if not boss:
        return ""
    if boss not in ms or boss == mid:
        raise Problem("直属上级要选公司里的另一个人。")
    up, seen = boss, set()
    while up:                                 # 不能绕成一圈
        if up == mid:
            raise Problem("他现在是这个人的下级，不能反过来当上级。")
        if up in seen:
            break
        seen.add(up)
        up = (ms.get(up) or {}).get("boss")
    return boss


def _archive(aid):
    d = agents.workspace(aid)
    for name, text in ((MEMORY, ""), ("CLAUDE.md", CLAUDE_MD)):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
    return d


def window(pf, mid):
    """员工的固定对话窗口：没有（或者被标成已完成、删了）就开一个。"""
    c = get(pf)
    m = (c.get("members") or {}).get(mid)
    if not m or m.get("kind") == "human":
        raise Problem("找不到这个员工。")
    chats = pf.chats()
    cid = m.get("chat")
    if cid and cid in chats and not chats[cid].get("archived"):
        return {"chat": cid}
    if not agents.get(mid):
        raise Problem("找不到这个员工。")
    cid = pf.new_chat(agent=mid, title=agents.get(mid)["name"])
    chats = pf.chats()
    chats[cid].update(staff=mid, title_auto=False)
    pf.store.write(chats)
    m["chat"] = cid
    _write(pf, c)
    return {"chat": cid}


def hire(pf, fields):
    """加一个成员。AI 员工：name、title、role（岗位说明）、boss、avatar、base、model、effort；真人：account、title、boss。"""
    if not get(pf):                          # 第一次加人：公司不用先建
        save(pf, {})
    c = get(pf)
    ms = c.setdefault("members", {})
    title = " ".join(str(fields.get("title") or "").split())[:20]
    if fields.get("account"):
        from . import people
        acct = str(fields["account"])
        if acct == people.OWNER or acct not in (registry.read_state(pf.home).get("guests") or {}):
            raise Problem("找不到这个账号。")
        mid = "h:" + acct
        if mid in ms:
            raise Problem("他已经在公司里了。")
        ms[mid] = {"kind": "human", "account": acct, "title": title, "boss": _boss_ok(ms, mid, fields.get("boss")),
                   "created": registry.now()}
    else:
        boss = _boss_ok(ms, None, fields.get("boss"))
        try:
            a = agents.create(staff=True, **{k: fields.get(k) for k in ("name", "role", "avatar", "base", "model", "effort") if fields.get(k)})
        except ValueError as e:
            raise Problem(str(e))
        mid = a["id"]
        _archive(mid)
        ms[mid] = {"kind": "ai", "title": title, "boss": boss, "created": registry.now()}
    _write(pf, c)
    if not mid.startswith("h:"):
        window(pf, mid)
        c = get(pf)
    _sync_group(pf, c)
    _relaunch(pf)
    return {"id": mid}


def edit(pf, mid, fields):
    """改岗位、上级；员工还能改名字、头像、岗位说明、模型。"""
    c = get(pf)
    ms = c.get("members") or {}
    m = ms.get(mid)
    if not m:
        raise Problem("找不到这个人。")
    if "title" in fields:
        m["title"] = " ".join(str(fields.get("title") or "").split())[:20]
    if "boss" in fields:
        m["boss"] = _boss_ok(ms, mid, fields.get("boss"))
    if m.get("kind") != "human":
        own = {k: fields[k] for k in ("name", "role", "avatar", "model", "effort") if k in fields}
        if "name" in own and not str(own["name"] or "").strip():
            raise Problem("名字不能空着。")
        if own:
            if "name" in own:
                own["seal"] = agents.seal_of(own["name"])
            agents.update(mid, **own)
            if m.get("chat") in pf.chats() and "name" in own:
                chats = pf.chats()
                chats[m["chat"]]["title"] = agents.get(mid)["name"]
                pf.store.write(chats)
    _write(pf, c)
    _relaunch(pf)
    return {}


def fire(pf, mid):
    """请走一个成员。员工：对话窗口标为已完成，档案文件夹留着；它的下级改成向它的上级汇报。"""
    c = get(pf)
    ms = c.get("members") or {}
    m = ms.get(mid)
    if not m:
        raise Problem("找不到这个人。")
    chats = pf.chats()
    if m.get("kind") != "human" and any(v.get("agent") == mid and v.get("busy") for v in chats.values()):
        raise Problem("他还在干活，先停下再说。")
    for o in ms.values():
        if o.get("boss") == mid:
            o["boss"] = m.get("boss") or ""
    ms.pop(mid)
    _write(pf, c)
    _sync_group(pf, c)
    if m.get("kind") != "human":
        chats = pf.chats()
        for cid, v in chats.items():
            if v.get("agent") == mid and not v.get("archived"):
                v.update(archived=True, archived_at=time.time())
        pf.store.write(chats)
        agents.remove(mid)
    _relaunch(pf)
    return {}


def memory(pf, mid):
    """员工自己记的：{text, dir}。"""
    m = (get(pf).get("members") or {}).get(mid)
    if not m or m.get("kind") == "human" or not agents.get(mid):
        raise Problem("找不到这个员工。")
    d = _archive(mid)
    try:
        with open(os.path.join(d, MEMORY), encoding="utf-8") as f:
            text = f.read(200_000)
    except OSError:
        text = ""
    return {"text": text, "dir": d}


def candidates(pf):
    """能请进公司的真人：来过这台电脑的账号里还不在公司的。"""
    ms = get(pf).get("members") or {}
    guests = registry.read_state(pf.home).get("guests") or {}
    return {"people": [{"id": a, "name": g.get("name") or a} for a, g in guests.items() if "h:" + a not in ms]}


def forget_agent(pf, aid):
    """Agent 在别处被删了：从公司里拿掉。"""
    c = get(pf)
    if aid in (c.get("members") or {}):
        for o in c["members"].values():
            if o.get("boss") == aid:
                o["boss"] = c["members"][aid].get("boss") or ""
        c["members"].pop(aid)
        _write(pf, c)
        _sync_group(pf, c)


def migrate(pf):
    """#454 的公司不要了：大群标为已完成，公司记录删掉。Agent 不动。只做一次。"""
    st = registry.read_state(pf.home)
    old = st.get("companies")
    if not old:
        return
    chats = pf.chats()
    for c in old.values():
        g = chats.get(c.get("group") or "")
        if g:
            g.pop("company", None)
            g.update(archived=True, archived_at=time.time())
    pf.store.write(chats)
    registry.write_state(pf.home, companies=None)
