"""共享电脑（#154）：服务器上的总控把一台电脑共享给别的账号，对方在这台电脑上有自己的一块空间。

总控转来的请求，发起的人不是这台电脑的主人时带 X-Account（账号编号）和 X-Account-Name（名字）；
只有经总控转来的（X-From-Peer）才认。被共享的人（下面叫「访客」）：
- 只能调 GUEST_GET / GUEST_POST 里的接口，其余一律拒绝（设备管理、管理员权限、渠道、升级、全局设置、项目、平台 Skill 管理……）
- 东西按账号分：Agent 记 account；对话跟着它的 Agent，临时对话记在 meta 的 account；定时任务跟着它交给的 Agent 或对话；
  知识库记在 _lib.json 的 account。访客只看得到、只改得了自己的；主人那边（没有 X-Account）看不到访客的
- 工作目录只能在给它的专属文件夹里（主目录下的 ccim共享/<名字>，第一次来时建，记在平台 state 的 guests）
- 推送带 _a（对话属于哪个访客），总控据此只推给那个账号

访客的 Agent 照样以这台电脑主人的系统用户身份运行，靠 Claude Code 自己的隔离圈住（`fence`）：命令行开沙箱（macOS 系统级，
不许绕过），读写文件的工具用权限规则；主目录里除了它的专属文件夹、平台 Skill、它自己的知识库，其余都不能读写。网络不限。
这是防越界，不等于单独的系统用户。Codex 的沙箱只拦写、不拦读，主人共享时勾了才给用（X-Account-Codex → guests 的 codex），
用时强制 workspace-write（只能写它自己的目录）。
"""
import os, re

from . import agents, registry

GUEST_GET = {
    "/api/state", "/api/chat", "/api/chat/sig", "/api/doc", "/api/agent", "/api/schedule", "/api/schedule/list",
    "/api/kb", "/api/kb/lib", "/api/kb/entry", "/api/kb/search", "/api/skills", "/api/skills/detail", "/api/skills/file",
    "/api/agent/skills", "/api/agent/skill", "/api/agent/skill/file", "/api/board", "/api/dirs", "/api/file", "/api/file/part",
    "/api/job", "/api/logs",
}
GUEST_POST = {
    "/api/upload", "/api/chat/new", "/api/chat/send", "/api/chat/stop", "/api/chat/skip", "/api/chat/action", "/api/chat/set",
    "/api/chat/delete", "/api/chat/mark", "/api/chat/seen", "/api/chat/dismiss", "/api/chat/kbs", "/api/chat/chapter",
    "/api/chat/fork", "/api/chat/rewind_preview", "/api/chat/rewind", "/api/chat/members", "/api/chat/note",
    "/api/chat/mention", "/api/chat/mention/stop", "/api/doc/save",
    "/api/kb/lib/save", "/api/kb/lib/delete", "/api/kb/entry/save", "/api/kb/entry/delete", "/api/kb/entry/move",
    "/api/agent/save", "/api/agent/delete", "/api/agent/pin",
    "/api/schedule/save", "/api/schedule/toggle", "/api/schedule/delete", "/api/schedule/run", "/api/schedule/try",
    "/api/agent/connect", "/api/channel/start", "/api/channel/stop", "/api/channel/restart", "/api/channel/disconnect",
    "/api/chat/continue",
}


class Denied(Exception):
    pass


def account_of(headers):
    """这次请求是哪个访客发的；这台电脑的主人（包括本机网页）返回 None。"""
    if not headers.get("X-From-Peer"):
        return None
    return (headers.get("X-Account") or "").strip() or None


# ---------- 东西属于谁 ----------

def of_agent(a):
    return (a or {}).get("account") or None


def of_chat(m):
    if not m:
        return None
    if m.get("agent"):
        return of_agent(agents.get(m["agent"]))
    return m.get("account") or None


def of_schedule(pf, s):
    t = (s or {}).get("target") or {}
    if t.get("kind") == "agent":
        return of_agent(agents.get(t.get("id") or ""))
    if t.get("kind") in ("chat", "group"):
        return of_chat(pf.chats().get(t.get("id") or ""))
    return None


def chat_owner(pf, cid):
    if not cid or str(cid).startswith("x:"):
        return None
    return of_chat(pf.chats().get(cid))


# ---------- 访客的专属文件夹 ----------

def _safe(name):
    return re.sub(r"[/\\:\x00]", "_", (name or "").strip()).strip(".")[:20]


def root(pf, acct, name=None):
    """访客的专属文件夹（没有就建）。名字只在第一次用，之后改名也不搬。"""
    guests = registry.read_state(pf.home).get("guests") or {}
    g = guests.get(acct) or {}
    if not g.get("root"):
        base = os.path.join(os.path.expanduser("~"), "ccim共享")
        safe = _safe(name) or acct
        d, n = os.path.join(base, safe), 2
        taken = {x.get("root") for x in guests.values()}
        while d in taken:
            d, n = os.path.join(base, f"{safe}{n}"), n + 1
        g = guests[acct] = {"root": d, "name": name or ""}
        registry.write_state(pf.home, guests=guests)
    os.makedirs(g["root"], exist_ok=True)
    return os.path.realpath(g["root"])


def remember(pf, acct, name, codex):
    """每次访客来都记一下：专属文件夹（第一次建）、主人现在让不让它用 Codex（飞书、微信的进程读这里）。"""
    root(pf, acct, name)
    guests = registry.read_state(pf.home).get("guests") or {}
    if bool(guests[acct].get("codex")) != bool(codex):
        guests[acct]["codex"] = bool(codex)
        registry.write_state(pf.home, guests=guests)


def codex_ok(acct):
    from . import platform
    return bool(((registry.read_state(platform.home()).get("guests") or {}).get(acct) or {}).get("codex"))


def inside(path, top):
    p = os.path.realpath(os.path.expanduser(path))
    return p == top or p.startswith(top + os.sep)


def guest_dir(pf, acct, name, path):
    """访客填的文件夹：只能在专属文件夹里，空的就用专属文件夹。"""
    top = root(pf, acct, name)
    if not (path or "").strip():
        return top
    if not inside(path, top):
        raise Denied("只能选你的文件夹里面的。")
    return os.path.realpath(os.path.expanduser(path))


# ---------- 访客的请求 ----------

def _mine_chat(pf, acct, cid):
    """网页对话，或者它的 Agent 在飞书、微信里的对话（x:，终端里的也算，在它的专属文件夹里）。"""
    if cid and str(cid).startswith("x:"):
        try:
            agent = pf._external(cid).get("agent")
        except Exception:
            agent = None
        if not agent or of_agent(agents.get(agent)) != acct:
            raise Denied("找不到这个对话。")
        return
    if not cid or cid not in pf.chats() or chat_owner(pf, cid) != acct:
        raise Denied("找不到这个对话。")


def procs_of(acct):
    """访客的 Agent 接的飞书、微信进程：{进程: Agent 的目录}。"""
    out = {}
    for aid, a in agents.all_agents().items():
        if of_agent(a) == acct:
            for kind, proc, path in agents.linked_paths(aid):
                out[proc] = path
    return out


def _mine_proc(acct, proc, path=None):
    mine = procs_of(acct)
    if proc not in mine or (path is not None and os.path.realpath(path) != os.path.realpath(mine[proc])):
        raise Denied("找不到这个渠道。")


def _mine_agent(acct, aid):
    if of_agent(agents.get(aid or "")) != acct:
        raise Denied("找不到这个 Agent。")


def _mine_lib(acct, lib):
    from . import kb
    try:
        info = kb._read_lib(lib or "")
    except ValueError:
        info = None
    if not info or (info.get("account") or None) != acct:
        raise Denied("找不到这个知识库。")


def check(pf, acct, name, method, path, q, body):
    """访客能不能这样做：不能就抛 Denied。会就地改 body（工作目录、账号这些由这里定，不信网页传来的）。"""
    if (path not in GUEST_GET) if method == "GET" else (path not in GUEST_POST):
        raise Denied("这台电脑是共享给你的，这个操作只有电脑的主人能做。")
    b = body if isinstance(body, dict) else {}
    if method == "GET":
        if path in ("/api/chat", "/api/chat/sig"):
            _mine_chat(pf, acct, q.get("id"))
        elif path in ("/api/doc", "/api/file", "/api/file/part"):
            _mine_chat(pf, acct, q.get("chat"))
        elif path in ("/api/agent", "/api/agent/skills", "/api/agent/skill", "/api/agent/skill/file"):
            _mine_agent(acct, q.get("id"))
        elif path == "/api/schedule":
            if of_schedule(pf, pf.schedules.all().get(q.get("id") or "")) != acct:
                raise Denied("找不到这个定时任务。")
        elif path == "/api/kb/lib":
            _mine_lib(acct, q.get("id"))
        elif path == "/api/kb/entry":
            _mine_lib(acct, q.get("lib"))
        elif path == "/api/logs":
            _mine_proc(acct, q.get("proc"))
        elif path == "/api/job":
            from . import web as console
            j = console.JOBS.get(q.get("id") or "")
            if not j or not j.agent_id or of_agent(agents.get(j.agent_id)) != acct:
                raise Denied("找不到这次接入。")
        return
    if path == "/api/agent/connect":
        _mine_agent(acct, b.get("id"))
    elif path in ("/api/channel/start", "/api/channel/stop", "/api/channel/restart"):
        _mine_proc(acct, b.get("proc"))
    elif path == "/api/channel/disconnect":
        _mine_proc(acct, b.get("proc"), b.get("path") or "")
    if path.startswith("/api/chat/") and path != "/api/chat/new":
        _mine_chat(pf, acct, b.get("id"))
    if path == "/api/chat/new":
        if b.get("agent"):
            _mine_agent(acct, b["agent"])
        else:                                 # 临时对话：只能用 Claude Code，没选文件夹就在专属文件夹里
            t = b.get("temp") if isinstance(b.get("temp"), dict) else {}
            t["dir"] = guest_dir(pf, acct, name, t.get("dir") or "")
            if t.get("base") != "codex" or not codex_ok(acct):
                t["base"] = "claude"
            b["temp"] = t
    elif path == "/api/chat/members":
        for aid in b.get("add") or []:
            _mine_agent(acct, aid)
    elif path == "/api/chat/mention":
        for aid in b.get("to") or []:
            _mine_agent(acct, aid)
    elif path == "/api/chat/kbs":
        for lib in b.get("kbs") or []:
            _mine_lib(acct, lib)
    elif path == "/api/doc/save":
        _mine_chat(pf, acct, b.get("chat"))
    elif path in ("/api/kb/lib/save", "/api/kb/lib/delete"):
        if b.get("id"):
            _mine_lib(acct, b["id"])
    elif path.startswith("/api/kb/entry/"):
        _mine_lib(acct, b.get("lib"))
        if path == "/api/kb/entry/move":
            _mine_lib(acct, b.get("to"))
    elif path in ("/api/agent/save", "/api/agent/delete", "/api/agent/pin"):
        if b.get("id"):
            _mine_agent(acct, b["id"])
        if path == "/api/agent/save":
            f = b.get("fields") if isinstance(b.get("fields"), dict) else {}
            f.pop("account", None)
            if not b.get("id") and not (f.get("dir") or "").strip():     # 每个 Agent 在专属文件夹里有自己的一个
                f["dir"] = os.path.join(root(pf, acct, name), _safe(f.get("name") or "") or "Agent")
                os.makedirs(f["dir"], exist_ok=True)
            elif "dir" in f:
                f["dir"] = guest_dir(pf, acct, name, f.get("dir") or "")
            f["chrome"] = False
            if f.get("base") == "codex" and not codex_ok(acct):
                raise Denied("这台电脑上只能用 Claude Code。")
            if not b.get("id"):
                f["account"] = acct
            b["fields"] = f
    elif path.startswith("/api/schedule/"):
        if path in ("/api/schedule/save", "/api/schedule/try"):
            f = b.get("fields") if isinstance(b.get("fields"), dict) else {}
            if b.get("id") and of_schedule(pf, pf.schedules.all().get(b["id"])) != acct:
                raise Denied("找不到这个定时任务。")
            if of_schedule(pf, {"target": f.get("target") or {}}) != acct or (f.get("target") or {}).get("kind") == "project":
                raise Denied("选一下交给哪个 Agent。")
            b["fields"] = f
        elif of_schedule(pf, pf.schedules.all().get(b.get("id") or "")) != acct:
            raise Denied("找不到这个定时任务。")


# ---------- 给谁看什么 ----------

def filter_state(pf, st, acct):
    """state 里只留这个人的（主人 acct=None：去掉访客的）。终端、飞书、微信里的对话只给主人。"""
    owners = owner_map(pf)

    def chat_ok(c):
        cid = str(c.get("id") or "")
        if cid.startswith("x:"):                  # 飞书、微信、终端里的：跟着它的 Agent
            return of_agent(agents.get(c.get("agent") or "")) == acct
        return owners.get(cid) == acct

    st["agents"] = [a for a in st.get("agents") or [] if (a.get("account") or None) == acct]
    st["chats"] = [c for c in st.get("chats") or [] if chat_ok(c)]
    st["schedules"] = [s for s in st.get("schedules") or [] if of_schedule(pf, s) == acct]
    if acct:
        st["projects"] = []
        st["global"] = {}
        st["ccim"] = {k: v for k, v in (st.get("ccim") or {}).items() if k != "dev"}   # 开发页只给电脑主人
        st["guest"] = {"root": root(pf, acct), "codex": codex_ok(acct)}
    return st


def filter_libs(libs, acct):
    return [x for x in libs if (x.get("account") or None) == acct]


_map = {"at": 0, "ver": None, "owners": {}}


def owner_map(pf):
    """对话编号 → 属于哪个访客（主人的是 None）。推送很频繁，缓存几秒；本进程写过对话就重算。"""
    import time
    ver = getattr(pf.store, "ver", 0)
    if time.time() - _map["at"] > 5 or _map["ver"] != ver:
        acc = {aid: of_agent(a) for aid, a in agents.all_agents().items()}
        _map["owners"] = {cid: (acc.get(m["agent"]) if m.get("agent") else m.get("account") or None)
                          for cid, m in pf.chats().items()}
        _map["at"], _map["ver"] = time.time(), ver
    return _map["owners"]


def tag(pf, ev):
    """推送带上它属于哪个访客（_a），总控只推给那个账号。"""
    try:
        cid = ev.get("chat")
        a = None
        if cid and not str(cid).startswith("x:"):
            a = owner_map(pf).get(cid)
        elif ev.get("type") == "schedule":
            a = of_schedule(pf, pf.schedules.all().get(ev.get("schedule") or ""))
        if a:
            ev["_a"] = a
    except Exception:
        pass
    return ev


# ---------- 访客的 Agent 能碰的范围 ----------

SANDBOX = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
           "network": {"allowedDomains": ["*"]}}


def guest_root(acct):
    """访客的专属文件夹（不在平台进程里时用：飞书、微信的进程）。还没建过返回 None。"""
    from . import platform
    g = (registry.read_state(platform.home()).get("guests") or {}).get(acct) or {}
    return os.path.realpath(g["root"]) if g.get("root") else None


def fence(acct):
    """访客的会话：(沙箱设置, 权限规则)。主目录下除了专属文件夹、~/.ccim 里的平台 Skill 和它自己的知识库，都不许读写。"""
    from . import kb
    home = os.path.realpath(os.path.expanduser("~"))
    ccim_home = os.path.realpath(registry.HOME)
    mine = guest_root(acct)

    def kids(d, keep=()):
        try:
            return [os.path.join(d, n) for n in os.listdir(d) if n not in keep]
        except OSError:
            return []
    deny = kids(home, ("ccim共享", os.path.basename(ccim_home)))
    deny += kids(ccim_home, ("kb", "skills"))
    deny += [p for p in kids(os.path.join(home, "ccim共享")) if os.path.realpath(p) != mine]
    deny += [os.path.join(kb.root(), x["id"]) for x in kb.libs() if (x.get("account") or None) != acct]
    deny.append("/Volumes")
    rules = []
    for p in deny:
        p = os.path.realpath(p)
        for t in ("Read", "Edit"):
            rules += [f"{t}(/{p})", f"{t}(/{p}/**)"]
    return dict(SANDBOX), {"permissions": {"deny": rules}}


def may_send(acct, path):
    """访客的 Agent 用「发文件」只能发专属文件夹里的。"""
    mine = guest_root(acct)
    return bool(mine) and inside(path, mine)
