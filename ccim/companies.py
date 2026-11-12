"""一人公司（#454）：把 Agent 分到公司里。一个公司有名字、颜色、公司说明和成员，还有一个公司大群。

存在打开网页的这台电脑的平台 state.json 的 companies（{编号: {name, color, note, members, group, created}}），
只在主循环里改。一个 Agent 只在一个公司。公司大群是一个不挂主 Agent 的群聊（meta 有 company），成员就是公司里的 Agent，
@ 谁谁回答，不 @ 只记下（Platform.send）。公司说明同步成大群的群聊说明，公司里的 Agent 在网页对话里干活时也带上（Platform._bridge）。
"""
import time
import uuid

from . import agents, registry

COLORS = ("qing", "zhe", "zhu", "lu", "dai", "huang")
# 内置公司头像（#459），图片在 static/companies/；顺序和网页里的选项一致，新增的往后追加编号
AVATARS = ("01-tower", "02-lighthouse", "03-rocket", "04-workshop", "05-study", "06-laboratory", "07-sailboat",
           "08-balloon", "09-castle", "10-greenhouse", "11-telescope", "12-factory", "13-cafe", "14-studio",
           "15-postoffice", "16-cabin")


class Problem(Exception):
    pass


def all_(pf):
    return registry.read_state(pf.home).get("companies") or {}


def _write(pf, data):
    registry.write_state(pf.home, companies=data)


def of_agent(pf, aid):
    """这个 Agent 在哪个公司：(编号, 记录) 或 (None, None)。"""
    for coid, c in all_(pf).items():
        if aid in (c.get("members") or []):
            return coid, c
    return None, None


def note_for(pf, aid):
    """公司说明：接在 Agent 的角色说明后面。"""
    _, c = of_agent(pf, aid)
    if not c or not (c.get("note") or "").strip():
        return ""
    return f"\n\n## 你所在的公司「{c['name']}」\n{c['note'].strip()}"


def view(pf):
    """给网页：公司列表，按建的先后。"""
    chats = pf.chats()
    out = []
    for coid, c in sorted(all_(pf).items(), key=lambda x: x[1].get("created") or ""):
        g = chats.get(c.get("group") or "") or {}
        out.append({"id": coid, "name": c["name"], "color": c.get("color") or "qing",
                    "avatar": c.get("avatar") if c.get("avatar") in AVATARS else None, "note": c.get("note") or "",
                    "members": list(c.get("members") or []), "group": c.get("group") if g else None,
                    "group_active": g.get("last_active")})
    return out


def _clean(fields):
    name = " ".join(str(fields.get("name") or "").split())[:20]
    color = fields.get("color") if fields.get("color") in COLORS else None
    note = str(fields.get("note") or "").strip()[:4000]
    avatar = fields.get("avatar") if fields.get("avatar") in AVATARS else None   # 空就是文字头像
    return name, color, note, avatar


def _group(pf, coid, c):
    """公司大群：没有就建一个。"""
    gid = c.get("group")
    if gid and gid in pf.chats():
        return gid
    gid = pf.new_chat(title=f"{c['name']} 大群")
    chats = pf.chats()
    chats[gid].update(company=coid, title_auto=False)
    pf.store.write(chats)
    c["group"] = gid
    return gid


def _sync_group(pf, c, title=False):
    """大群的成员、群聊说明、标题跟着公司走。"""
    from . import groups
    gid = c.get("group")
    chats = pf.chats()
    if not gid or gid not in chats:
        return
    have = groups.members(chats[gid])
    want = list(c.get("members") or [])
    add = [a for a in want if a not in have]
    drop = [a for a in have if a not in want]
    if add or drop:
        try:
            groups.set_members(pf, gid, add=add, remove=drop)
        except ValueError:                    # 别的电脑上的 Agent 找不到（离线、删了）：先加能加的
            groups.set_members(pf, gid, add=[a for a in add if agents.get(a)], remove=drop)
    if (chats[gid].get("note") or "") != (c.get("note") or ""):
        groups.set_note(pf, gid, c.get("note") or "")
    if title:
        chats = pf.chats()
        chats[gid]["title"] = f"{c['name']} 大群"
        pf.store.write(chats)


def save(pf, coid, fields):
    """新建（coid 为空）或改名、换颜色、换头像、改说明。返回 {id, group}。"""
    name, color, note, avatar = _clean(fields)
    data = all_(pf)
    if coid:
        c = data.get(coid)
        if not c:
            raise Problem("找不到这个公司。")
        renamed = "name" in fields and name and name != c["name"]
        if "name" in fields:
            if not name:
                raise Problem("给公司起个名字。")
            c["name"] = name
        if color:
            c["color"] = color
        if "note" in fields:
            c["note"] = note
        if "avatar" in fields:
            c["avatar"] = avatar
    else:
        if not name:
            raise Problem("给公司起个名字。")
        coid = "co-" + uuid.uuid4().hex[:8]
        used = {x.get("color") for x in data.values()}
        c = data[coid] = {"name": name, "color": color or next((k for k in COLORS if k not in used), COLORS[0]),
                          "note": note, "avatar": avatar, "members": [], "created": registry.now()}
        renamed = False
    _group(pf, coid, c)
    _write(pf, data)
    _sync_group(pf, c, title=renamed)
    return {"id": coid, "group": c["group"]}


def join(pf, aid, coid):
    """把 Agent 放进某个公司（coid 为空就是移出公司）。一个 Agent 只在一个公司。"""
    data = all_(pf)
    if coid and coid not in data:
        raise Problem("找不到这个公司。")
    if coid and not agents.get(aid) and not pf.peers.agent_home(aid):
        raise Problem("找不到这个 Agent。")
    touched = []
    for k, c in data.items():
        ms = list(c.get("members") or [])
        if k == coid and aid not in ms:
            c["members"] = ms + [aid]
            touched.append(c)
        elif k != coid and aid in ms:
            c["members"] = [a for a in ms if a != aid]
            touched.append(c)
    if coid:
        _group(pf, coid, data[coid])
    _write(pf, data)
    for c in touched:
        _sync_group(pf, c)
    return {}


def delete(pf, coid):
    """删公司：Agent 不删，回到没有公司；大群标为已完成，记录留着。"""
    data = all_(pf)
    c = data.pop(coid, None)
    if not c:
        raise Problem("找不到这个公司。")
    _write(pf, data)
    chats = pf.chats()
    gid = c.get("group")
    if gid in chats:
        chats[gid].update(archived=True, archived_at=time.time())
        chats[gid].pop("company", None)
        pf.store.write(chats)
    return {}


def forget_agent(pf, aid):
    """Agent 删掉了：从公司里拿掉。"""
    if of_agent(pf, aid)[0]:
        join(pf, aid, None)
