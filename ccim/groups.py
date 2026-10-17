"""群聊（#15）：Agent 的对话里拉别的 Agent 进来就是群聊，取代「项目」入口。

群聊就是一个普通的网页对话，meta 里多了 members（除主 Agent 以外的成员，可以在别的电脑上）、note（群聊说明）。
不 @ 时和主 Agent（对话本来的那个 Agent）说；@ 谁就走 mentions（每人一个子对话，带上之前的内容），所以群里的
Agent 看得到之前说过的话，只在被 @ 时回答。群聊说明改了以后，各人下一次被问到时带上新的（note_seen 记各人看到的版本）。

现有项目各复制成一个群聊（只复制，项目页不动）：第一个成员当主 Agent，接着它在项目里的会话（fork 那个会话），
项目名当群名，项目目录当群聊的工作目录，项目记录（task / reply）转成群聊记录（user / mention）。
"""
import logging, time

from . import agents, registry

log = logging.getLogger("ccim.groups")
MAIN = "__main__"                             # note_seen 里主 Agent 的键


def members(m):
    return [a for a in (m.get("members") or []) if a != m.get("agent")]


def note_prefix(pf, cid, who):
    """群聊说明改过、这个 Agent 还没看过新的：返回要放在消息前面的一段，并记下已看过。"""
    chats = pf.chats()
    m = chats.get(cid) or {}
    note = (m.get("note") or "").strip()
    if not note or not members(m):
        return ""
    seen = m.get("note_seen") or {}
    if seen.get(who, 0) >= m.get("note_at", 0):
        return ""
    seen[who] = time.time()
    m["note_seen"] = seen
    pf.store.write(chats)
    return f"（这是一个群聊，群聊说明如下，回答时照着做）\n{note}\n\n——\n\n"


def set_members(pf, cid, add=None, remove=None):
    chats = pf.chats()
    m = chats.get(cid)
    if not m:
        raise ValueError("找不到这个对话。")
    ms = list(members(m))
    for aid in add or []:
        if aid == m.get("agent") or aid in ms:
            continue
        if not agents.get(aid) and not pf.peers.agent_home(aid):
            raise ValueError("找不到这个 Agent。")
        ms.append(aid)
    if remove:
        ms = [a for a in ms if a not in remove]
    m["members"] = ms
    pf.store.write(chats)
    return ms


def set_note(pf, cid, text):
    chats = pf.chats()
    m = chats.get(cid)
    if not m:
        raise ValueError("找不到这个对话。")
    m["note"] = (text or "").strip()[:4000]
    m["note_at"] = time.time()
    pf.store.write(chats)
    return {}


def migrate_projects(pf):
    """每个项目复制成一个群聊（只做一次，记在平台 state 的 groups_from_projects）。"""
    st = registry.read_state(pf.home)
    done = dict(st.get("groups_from_projects") or {})
    projects = st.get("projects") or {}
    chats = pf.chats()
    changed = False
    for pid, p in projects.items():
        if pid in done:
            continue
        ms = [a for a in p.get("members") or [] if agents.get(a)] + [a for a in p.get("members") or [] if not agents.get(a)]
        main = next((a for a in ms if agents.get(a)), None)    # 主 Agent 要在这台电脑上
        if not main:
            continue
        import uuid
        cid = "w-" + uuid.uuid4().hex[:10]
        old = chats.get(f"p-{pid}-{main}") or {}
        rec = {"type": "p2p", "agent": main, "title": p.get("name") or "群聊", "members": [a for a in ms if a != main],
               "created": p.get("created") or registry.now(), "last_active": p.get("last_active") or registry.now(),
               "from_project": pid}
        if p.get("dir"):
            rec["cwd"] = p["dir"]
        if old.get("session_id"):            # 主 Agent 接着它在项目里的会话
            rec["fork_from"] = old["session_id"]
        chats[cid] = rec
        logs = pf.channel.logs
        for r in logs.read(f"project-{pid}"):
            if r.get("type") == "task":
                logs.append(cid, {"type": "user", "id": r["id"], "text": r.get("text") or "", "files": [],
                                  "to": [a for a in r.get("to") or [] if a != main] or None, "at": r.get("at")})
                mine = next((x for x in logs.read(f"project-{pid}") if x.get("type") == "reply" and x.get("task") == r["id"]
                             and x.get("agent") == main), None)
                if mine and mine.get("text") and len(r.get("to") or []) == 1:
                    logs.append(cid, {"type": "answer", "text": mine["text"], "steps": mine.get("steps") or [],
                                      "state": "done" if mine.get("state") == "done" else mine.get("state"),
                                      "at": mine.get("at")})
            elif r.get("type") == "reply" and not (r.get("agent") == main and _solo(logs, pid, r.get("task"))):
                logs.append(cid, {"type": "mention", "id": f"{r['task']}:{r['agent']}", "msg": r["task"], "agent": r["agent"],
                                  "chat": r.get("chat"), "machine": r.get("machine"),
                                  "state": "done" if r.get("state") == "done" else (r.get("state") if r.get("state") != "queued" else "lost"),
                                  "text": r.get("text") or "", "steps": r.get("steps") or [], "elapsed": r.get("elapsed"),
                                  "at": r.get("at"), "done_at": r.get("at")})
        done[pid] = cid
        changed = True
        log.info("项目「%s」复制成群聊 %s", p.get("name"), cid)
    if changed:
        pf.store.write(chats)
        registry.write_state(pf.home, groups_from_projects=done)
        # 指向项目的定时任务改指向群聊
        st = registry.read_state(pf.home)
        sch = st.get("schedules") or {}
        for s in sch.values():
            t = s.get("target") or {}
            if t.get("kind") == "project" and t.get("id") in done:
                gid = done[t["id"]]
                main = chats[gid]["agent"]
                s["target"] = {"kind": "group", "id": gid, "agents": [a for a in t.get("agents") or [] if a != main]}
        registry.write_state(pf.home, schedules=sch)
    return done


def _solo(logs, pid, tid):
    """这件活只派给了一个人（主 Agent）：转成普通的一问一答，不是 @ 的回答。"""
    t = next((x for x in logs.read(f"project-{pid}") if x.get("type") == "task" and x.get("id") == tid), None)
    return bool(t) and len(t.get("to") or []) == 1
