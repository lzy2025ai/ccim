"""共享电脑（#154）：服务器上的总控按「账号 → 电脑 → Agent」给别的账号开权限，对方在这台电脑上有自己的一块地方。

总控转来的请求，发起的人不是这台电脑的主人时带 X-Account（账号编号）、X-Account-Name（账号名）、
X-Account-Share（主人给它的权限：agents 能用哪些主人的 Agent、own 能不能自己建 Agent、codex 能不能用 Codex、dir 共享文件夹）；
只有经总控转来的（X-From-Peer）才认。每次来都记到平台 state 的 guests（飞书、微信的进程、会话都读这里）。被共享的人（下面叫「访客」）：
- 只能调 GUEST_GET / GUEST_POST 里的接口，其余一律拒绝（设备管理、管理员权限、升级、全局设置、项目、平台 Skill 管理……）
- 东西按账号分：Agent 记 account；对话记 meta 的 account（没有就跟着它的 Agent；@ 子对话跟着主对话，定时任务的对话跟着任务）；
  定时任务记 account（没有就跟着交给的 Agent / 对话）；知识库记在 _lib.json。访客只看得到、只改得了自己的；主人看不到访客的
- 主人共享给它的 Agent：能开自己的对话、拉进自己的群聊，改不了设置、不能接飞书微信；对话里不给「看别的对话」「转交」
- 自己建的 Agent、临时对话都在共享文件夹里（默认 ~/.ccim/shared/<账号名>，主人可以指定）
- 推送带 _a（对话属于哪个访客），总控据此只推给那个账号

访客的会话仍以这台电脑主人的系统用户身份运行，靠 Claude Code 自己的隔离圈住（`fence`）：命令行开沙箱（macOS 系统级，不许绕过），
读写文件的工具用权限规则；主目录里只留共享文件夹、这个会话的工作目录（共享的 Agent 在它自己的目录干活）、平台 Skill、它自己的知识库。
网络不限。这是防越界，不等于单独的系统用户。Codex 的沙箱只拦写不拦读，主人勾了才给用，用时强制 workspace-write。
"""
import json, os, re, time

from . import agents, registry

GUEST_GET = {
    "/api/state", "/api/chat", "/api/chat/sig", "/api/doc", "/api/agent", "/api/schedule", "/api/schedule/list",
    "/api/kb", "/api/kb/lib", "/api/kb/entry", "/api/kb/search", "/api/skills", "/api/skills/detail", "/api/skills/file",
    "/api/agent/skills", "/api/agent/skill", "/api/agent/skill/file", "/api/board", "/api/board/day", "/api/dirs", "/api/file", "/api/file/part",
    "/api/job", "/api/logs", "/api/skills/market", "/api/ws/ls",
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
    "/api/skills/install", "/api/skills/toggle", "/api/skills/delete", "/api/skills/update", "/api/skills/file/save", "/api/helper",
    "/api/chat/side",
    "/api/ws/new", "/api/ws/rename", "/api/ws/move", "/api/ws/trash", "/api/ws/restore", "/api/ws/put",
}


class Denied(Exception):
    pass


def account_of(headers):
    """这次请求是哪个访客发的；这台电脑的主人（包括本机网页）返回 None。"""
    if not headers.get("X-From-Peer"):
        return None
    return (headers.get("X-Account") or "").strip() or None


def share_of(headers):
    from urllib.parse import unquote
    try:
        o = json.loads(unquote(headers.get("X-Account-Share") or "") or "{}")
    except ValueError:
        o = {}
    return o if isinstance(o, dict) else {}


# ---------- 访客的记录（平台 state 的 guests） ----------

def _plat():
    from . import platform
    return platform.home()


def guest(acct):
    return (registry.read_state(_plat()).get("guests") or {}).get(acct) or {}


def _safe(name):
    return re.sub(r"[/\\:\x00]", "_", (name or "").strip()).strip(".")[:20]


def _bad_root(d):
    """主人指定的共享文件夹不能是整个硬盘、主目录（或它的上级）、~/.ccim 本身。"""
    home = os.path.realpath(os.path.expanduser("~"))
    ccim = os.path.realpath(registry.HOME)
    return d in ("/", home, ccim) or home.startswith(d + os.sep) or ccim.startswith(d + os.sep)


def remember(pf, acct, name, opts):
    """访客每次来都记一下：账号名、主人给的权限、共享文件夹（没有就建）。返回这个访客的记录。"""
    guests = registry.read_state(pf.home).get("guests") or {}
    g = dict(guests.get(acct) or {})
    d = (opts.get("dir") or "").strip()
    d = os.path.realpath(os.path.expanduser(d)) if d.startswith(("/", "~")) else ""
    if not d or _bad_root(d):
        d = os.path.join(os.path.realpath(registry.HOME), "shared", _safe(name) or acct)
    new = dict(g, name=name or g.get("name") or "", root=d, codex=bool(opts.get("codex")), own=bool(opts.get("own", True)),
               agents=[a for a in opts.get("agents") or [] if isinstance(a, str)])
    if new != guests.get(acct):
        guests[acct] = new
        registry.write_state(pf.home, guests=guests)
    os.makedirs(d, exist_ok=True)
    return new


def root(acct):
    g = guest(acct)
    return os.path.realpath(g["root"]) if g.get("root") else None


def codex_ok(acct):
    return bool(guest(acct).get("codex"))


def own_ok(acct):
    return bool(guest(acct).get("own", True))


def shared_agents(acct):
    """主人共享给它的 Agent（还在、而且是主人的）。"""
    return [a for a in guest(acct).get("agents") or [] if agents.get(a) and not of_agent(agents.get(a))]


def inside(path, top):
    p = os.path.realpath(os.path.expanduser(path))
    return bool(top) and (p == top or p.startswith(top + os.sep))


def guest_dir(acct, path):
    """访客填的文件夹：只能在共享文件夹里，空的就用共享文件夹。"""
    top = root(acct)
    if not (path or "").strip():
        return top
    if not inside(path, top):
        raise Denied("只能选你的文件夹里面的。")
    return os.path.realpath(os.path.expanduser(path))


# ---------- 东西属于谁 ----------

def of_agent(a):
    return (a or {}).get("account") or None


def of_schedule(pf, s):
    s = s or {}
    if s.get("account"):
        return s["account"]
    t = s.get("target") or {}
    if t.get("kind") == "agent":
        return of_agent(agents.get(t.get("id") or ""))
    if t.get("kind") in ("chat", "group"):
        return owner_map(pf).get(t.get("id") or "")
    return None


_map = {"at": 0, "ver": None, "owners": {}}


def owner_map(pf):
    """网页对话编号 → 属于哪个访客（主人的是 None）。推送很频繁，缓存几秒；本进程写过对话就重算。"""
    ver = getattr(pf.store, "ver", 0)
    if time.time() - _map["at"] > 5 or _map["ver"] != ver:
        acc = {aid: of_agent(a) for aid, a in agents.all_agents().items()}
        chats = pf.chats()
        sched = pf.schedules.all() if getattr(pf, "schedules", None) else {}

        def own(cid, m, depth=0):
            if "account" in m:
                return m["account"] or None
            if m.get("mention_of") and depth < 3:    # 群聊里 @ 的子对话：跟着群聊
                return own(m["mention_of"], chats.get(m["mention_of"]) or {}, depth + 1)
            if m.get("schedule") and sched.get(m["schedule"], {}).get("account"):
                return sched[m["schedule"]]["account"]
            return acc.get(m.get("agent")) if m.get("agent") else None
        _map["owners"] = {cid: own(cid, m) for cid, m in chats.items()}
        _map["at"], _map["ver"] = time.time(), ver
    return _map["owners"]


def chat_owner(pf, cid):
    if not cid or str(cid).startswith("x:"):
        return None
    return owner_map(pf).get(cid)


# ---------- 访客的请求 ----------

def _mine_chat(pf, acct, cid):
    """网页对话，或者它自己的 Agent 在飞书、微信、终端里的对话（x:）。"""
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
    """访客自己的 Agent 接的飞书、微信进程：{进程: Agent 的目录}。"""
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
    """它自己建的 Agent（能改、能删、能接飞书微信）。"""
    if not aid or of_agent(agents.get(aid)) != acct:
        raise Denied("找不到这个 Agent。")


def _usable_agent(acct, aid):
    """能和它对话：自己建的，或者主人共享给它的。"""
    if not aid or not (of_agent(agents.get(aid)) == acct or aid in shared_agents(acct)):
        raise Denied("找不到这个 Agent。")


def _mine_lib(acct, lib):
    from . import kb
    try:
        info = kb._read_lib(lib or "")
    except ValueError:
        info = None
    if not info or (info.get("account") or None) != acct:
        raise Denied("找不到这个知识库。")


def _base_ok(acct, aid=None, base=None):
    base = base or (agents.get(aid or "") or {}).get("base") or "claude"
    if base == "codex" and not codex_ok(acct):
        raise Denied("这台电脑上只能用 Claude Code。")


def check(pf, acct, method, path, q, body):
    """访客能不能这样做：不能就抛 Denied。会就地改 body（工作目录、账号这些由这里定，不信网页传来的）。"""
    if (path not in GUEST_GET) if method == "GET" else (path not in GUEST_POST):
        raise Denied("这台电脑是共享给你的，这个操作只有电脑的主人能做。")
    b = body if isinstance(body, dict) else {}
    if method == "GET":
        if path in ("/api/chat", "/api/chat/sig"):
            _mine_chat(pf, acct, q.get("id"))
        elif path in ("/api/doc", "/api/file", "/api/file/part", "/api/ws/ls"):
            _mine_chat(pf, acct, q.get("chat"))
        elif path in ("/api/agent", "/api/agent/skills", "/api/agent/skill", "/api/agent/skill/file"):
            _usable_agent(acct, q.get("id"))
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
    if path == "/api/skills/install" and b.get("local"):     # 主人电脑上 ~/.claude/skills 里的不给它导
        raise Denied("这台电脑是共享给你的，这个操作只有电脑的主人能做。")
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
            _usable_agent(acct, b["agent"])
            _base_ok(acct, b["agent"])
        else:                                 # 临时对话：没选文件夹就在共享文件夹里
            t = b.get("temp") if isinstance(b.get("temp"), dict) else {}
            t["dir"] = guest_dir(acct, t.get("dir") or "")
            if t.get("base") != "codex" or not codex_ok(acct):
                t["base"] = "claude"
            b["temp"] = t
    elif path == "/api/chat/send":
        m = pf.chats().get(b.get("id") or "") or {}
        if m.get("agent"):
            _base_ok(acct, m["agent"])
    elif path == "/api/chat/members":
        for aid in b.get("add") or []:
            _usable_agent(acct, aid)
    elif path == "/api/chat/mention":
        for aid in b.get("to") or []:
            _usable_agent(acct, aid)
    elif path == "/api/chat/kbs":
        for lib in b.get("kbs") or []:
            _mine_lib(acct, lib)
    elif path == "/api/doc/save" or path.startswith("/api/ws/"):
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
        elif not own_ok(acct):
            raise Denied("这台电脑上不能自己建 Agent。")
        if path == "/api/agent/save":
            f = b.get("fields") if isinstance(b.get("fields"), dict) else {}
            f.pop("account", None)
            if not b.get("id") and not (f.get("dir") or "").strip():     # 每个 Agent 在共享文件夹里有自己的一个
                f["dir"] = os.path.join(root(acct), _safe(f.get("name") or "") or "Agent")
                os.makedirs(f["dir"], exist_ok=True)
            elif "dir" in f:
                f["dir"] = guest_dir(acct, f.get("dir") or "")
                os.makedirs(f["dir"], exist_ok=True)      # 网页上默认填的「共享文件夹/名字」还没建
            f["chrome"] = False
            _base_ok(acct, base=f.get("base"))
            if not b.get("id"):
                f["account"] = acct
            b["fields"] = f
    elif path.startswith("/api/schedule/"):
        if path in ("/api/schedule/save", "/api/schedule/try"):
            f = b.get("fields") if isinstance(b.get("fields"), dict) else {}
            if b.get("id") and of_schedule(pf, pf.schedules.all().get(b["id"])) != acct:
                raise Denied("找不到这个定时任务。")
            t = f.get("target") or {}
            if t.get("kind") == "agent":
                _usable_agent(acct, t.get("id"))
            elif t.get("kind") in ("chat", "group"):
                _mine_chat(pf, acct, t.get("id"))
            else:
                raise Denied("选一下交给哪个 Agent。")
            f["account"] = acct
            b["fields"] = f
        elif of_schedule(pf, pf.schedules.all().get(b.get("id") or "")) != acct:
            raise Denied("找不到这个定时任务。")


# ---------- 给谁看什么 ----------

def chat_filter(pf, acct):
    """(对话编号, 它的 Agent) 是不是这个人的（主人 acct=None：去掉访客的）。终端、飞书、微信里的对话跟着它的 Agent。
    列表分页要在挑前几条之前就按人过滤（#268），不然别人的对话会占掉名额。"""
    owners = owner_map(pf)

    def ok(cid, aid=None):
        cid = str(cid or "")
        if cid.startswith("x:"):
            return of_agent(agents.get(aid or "")) == acct
        return owners.get(cid) == acct
    return ok


def filter_state(pf, st, acct):
    """state 里只留这个人的（主人 acct=None：去掉访客的）。终端、飞书、微信里的对话跟着它的 Agent。"""
    ok = chat_filter(pf, acct)

    def chat_ok(c):
        return ok(c.get("id"), c.get("agent"))

    shared = set(shared_agents(acct)) if acct else set()
    st["agents"] = [view_agent(a, acct) for a in st.get("agents") or []
                    if (a.get("account") or None) == acct or a.get("id") in shared]
    st["chats"] = [c for c in st.get("chats") or [] if chat_ok(c)]
    st["schedules"] = [s for s in st.get("schedules") or [] if of_schedule(pf, s) == acct]
    if acct:
        g = guest(acct)
        st["projects"] = []
        st["global"] = {k: v for k, v in (st.get("global") or {}).items()     # 主人设的默认模型，只给它看（#154）
                        if k in ("model", "effort", "codex_model", "codex_effort", "archive_days")}
        st["ccim"] = {k: v for k, v in (st.get("ccim") or {}).items() if k != "dev"}   # 开发页只给电脑主人
        st.pop("perms", None)
        st["guest"] = {"root": root(acct), "codex": bool(g.get("codex")), "own": bool(g.get("own", True))}
    return st


def view_agent(a, acct):
    """访客看主人共享给它的 Agent：标上 shared，不给它看接了哪些飞书、微信和在干什么。"""
    if not acct or (a.get("account") or None) == acct:
        return a
    return dict(a, shared=True, channels=[], busy=False, doing=None)


def filter_agent_chats(pf, view, acct):
    """Agent 详情里的对话列表：只留这个人的。"""
    owners = owner_map(pf)
    mine = (view.get("account") or None) == acct
    view["chats"] = [c for c in view.get("chats") or []
                     if (mine if str(c.get("id", "")).startswith("x:") else owners.get(c.get("id")) == acct)]
    return view_agent(view, acct)


def filter_libs(libs, acct):
    return [x for x in libs if (x.get("account") or None) == acct]


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


# ---------- 访客的会话能碰的范围 ----------

SANDBOX = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
           "network": {"allowedDomains": ["*"]}}


def read_only(sandbox, cwd):
    """侧边对话（#196）的命令沙箱：主目录、工作目录、外接硬盘都不许写。"""
    sb = dict(sandbox or SANDBOX)
    fs = dict(sb.get("filesystem") or {})
    fs["denyWrite"] = list(fs.get("denyWrite") or []) + [os.path.realpath(os.path.expanduser("~")), os.path.realpath(cwd), "/Volumes"]
    sb["filesystem"] = fs
    return sb


def allowed_dirs(acct, cwd=None):
    """访客的会话能读写的地方：共享文件夹、它自己 Agent 的目录、这个会话的工作目录、平台 Skill、它自己的知识库。"""
    from . import kb
    sk = os.path.join(registry.HOME, "skills")      # 主人的平台 Skill、会话挂的插件目录、它自己装的那份（别的访客的不给）
    out = [root(acct), cwd, os.path.join(sk, "lib"), os.path.join(sk, "views"), os.path.join(sk, "guests", acct)]
    out += [a.get("dir") for a in agents.all_agents().values() if of_agent(a) == acct]
    out += [os.path.join(kb.root(), x["id"]) for x in kb.libs() if (x.get("account") or None) == acct]
    return sorted({os.path.realpath(p) for p in out if p})


def _deny_except(top, keep):
    """top 下面除了 keep（以及去 keep 路上经过的文件夹）以外的东西：一层层往下，只拦旁边的。"""
    out = []
    try:
        names = os.listdir(top)
    except OSError:
        return out
    for n in names:
        p = os.path.join(top, n)
        if any(k == p for k in keep):
            continue
        if any(k.startswith(p + os.sep) for k in keep):
            out += _deny_except(p, keep)
        else:
            out.append(p)
    return out


def fence(acct, cwd=None):
    """访客的会话：(沙箱设置, 权限规则)。主目录（和外接硬盘）里除了 allowed_dirs，都不许读写。"""
    home = os.path.realpath(os.path.expanduser("~"))
    keep = [k for k in allowed_dirs(acct, cwd) if k.startswith(home + os.sep)]
    deny = _deny_except(home, keep) + ["/Volumes"]
    rules = []
    for p in deny:
        p = os.path.realpath(p)
        for t in ("Read", "Edit"):
            rules += [f"{t}(/{p})", f"{t}(/{p}/**)"]
    return dict(SANDBOX), {"permissions": {"deny": rules}}


def may_send(acct, path, cwd=None):
    """访客的会话用「发文件」只能发它能读写的地方的文件。"""
    sk = os.path.realpath(os.path.join(registry.HOME, "skills"))
    return any(inside(path, d) for d in allowed_dirs(acct, cwd) if not d.startswith(sk + os.sep))
