"""Agent 登记表：~/.ccim/agents.json，{编号: Agent}。

Agent = 名字、印章（字和颜色）、角色说明、底座（claude / codex）、默认模型和思考深度、工作目录（可以没有）、
能不能用 Chrome（chrome：会话带 --chrome，接上本机 Claude in Chrome 扩展，只对 Claude 底座有用）。
没有工作目录的 Agent 在 ~/.ccim/agents/<编号>/ 里干活。

飞书、微信的登记还是按目录记的（registry.json、微信号的 projects），各带一个 agent_id 指回这里。
Agent 是设置的唯一来源：改了 Agent，同步写回它关联的飞书、微信登记（agent / model / effort / role），
飞书、微信进程里现有的代码照常从登记里读，不用改。
0.11 以前的「项目」第一次用到时自动迁移成 Agent（`migrate`）。
"""
import contextlib, fcntl, json, os, re, tempfile, time, uuid

from . import registry

COLORS = ("qing", "zhu", "huang", "lu", "zhe", "dai")
AVATARS = ("01-navigator", "02-coder", "03-designer", "04-writer", "05-researcher", "06-analyst",
           "07-engineer", "08-assistant", "09-explorer", "10-guardian", "11-maker", "12-planner",
           "13-spark", "14-archivist", "15-buddy", "16-sprout",
           "17-panda", "18-rabbit", "19-raccoon", "20-otter",
           "21-capybara", "22-red-panda", "23-koala", "24-tiger",
           "25-dragon", "26-whale", "27-axolotl", "28-hedgehog",
           "29-mushroom", "30-cloud", "31-sun", "32-catbot")
FIELDS = ("name", "seal", "color", "avatar", "base", "model", "effort", "role", "dir", "pinned", "chrome", "skills_off",
          "account")   # account：共享电脑上访客建的（#154）
SYNCED = {"base": "agent", "model": "model", "effort": "effort", "role": "role", "chrome": "chrome"}   # Agent 字段 → 登记里的字段


def _file():
    return os.path.join(registry.HOME, "agents.json")


@contextlib.contextmanager
def _locked():
    os.makedirs(registry.HOME, exist_ok=True)
    with open(os.path.join(registry.HOME, ".agents.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read():
    try:
        with open(_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data):
    fd, tmp = tempfile.mkstemp(dir=registry.HOME, prefix=".agents.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _file())


def all_agents():
    try:
        return registry.load_json(_file())    # 一次请求里只读一遍（registry.snapshot）；要改了写回去的用 _read
    except (OSError, ValueError):
        return {}


def get(aid):
    a = all_agents().get(aid)
    return dict(a, id=aid) if a else None


def seal_of(name):
    """印章上的字：名字里的前两个字（中文）或前两个字母。"""
    chars = [c for c in (name or "") if not c.isspace() and c not in "-_·.()（）「」"]
    han = [c for c in chars if "一" <= c <= "鿿"]
    if han:
        return "".join(han[:2])
    letters = [c for c in chars if c.isalpha()]
    return (letters[0].upper() if letters else "".join(chars[:1])) or "新"   # 英文名刻一个大写首字母


def _new_id(name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:20] or "agent"
    return f"{base}-{uuid.uuid4().hex[:4]}"


def clean(fields):
    out = {k: v for k, v in fields.items() if k in FIELDS}
    for k in ("model", "effort", "role", "dir"):
        if k in out:
            v = out[k]
            out[k] = (v.strip() or None) if isinstance(v, str) else (v or None)
    if out.get("dir"):
        out["dir"] = os.path.realpath(os.path.expanduser(out["dir"]))
    if "base" in out and out["base"] not in ("claude", "codex"):
        out["base"] = "claude"
    for k in ("pinned", "chrome"):
        if k in out:
            out[k] = bool(out[k])
    if "skills_off" in out:                   # 平台 Skill 里这个 Agent 不挂的（#101）
        out["skills_off"] = sorted({str(x) for x in out["skills_off"] or [] if isinstance(x, str)})
    if "color" in out and out["color"] not in COLORS:
        out["color"] = COLORS[0]
    if "avatar" in out:
        out["avatar"] = out["avatar"] if isinstance(out["avatar"], str) and out["avatar"] in AVATARS else None
    return out


def create(**fields):
    f = clean(fields)
    if not (f.get("name") or "").strip():
        raise ValueError("先给它起个名字")
    with _locked():
        data = _read()
        aid = _new_id(f["name"])
        f.setdefault("seal", seal_of(f["name"]))
        f.setdefault("color", COLORS[len(data) % len(COLORS)])
        f.setdefault("base", "claude")
        data[aid] = dict(f, created=registry.now())
        _write(data)
    return get(aid)


def update(aid, **fields):
    f = clean(fields)
    with _locked():
        data = _read()
        if aid not in data:
            raise KeyError(aid)
        data[aid].update(f)
        _write(data)
    a = get(aid)
    _sync(a)
    return a


def remove(aid):
    """删掉 Agent；它的飞书、微信接入要先断开（由调用方做）。工作目录、工作区里的文件不删。"""
    with _locked():
        data = _read()
        data.pop(aid, None)
        _write(data)


def workspace(aid):
    d = os.path.join(registry.HOME, "agents", aid)
    os.makedirs(d, exist_ok=True)
    return d


def cwd(a):
    """这个 Agent 在哪干活：工作目录，没有就用它自己的工作区。"""
    return a.get("dir") or workspace(a["id"])


# ---------- 和飞书、微信登记的关联 ----------

def linked_paths(aid):
    """这个 Agent 接了哪些渠道：[(渠道 feishu / weixin, 进程登记的路径, 项目路径)]。"""
    out = []
    for p, e in registry.all_projects().items():
        if e.get("kind") == "weixin":
            out += [("weixin", p, path) for path, s in (e.get("projects") or {}).items() if (s or {}).get("agent_id") == aid]
        elif e.get("agent_id") == aid:
            out.append(("feishu", p, p))
    return out


def for_path(path):
    """项目目录对应的 Agent（飞书、微信登记里的 agent_id）。"""
    path = os.path.realpath(path)
    e = registry.get(path)
    aid = e.get("agent_id") if e and e.get("kind") != "weixin" else None
    if not aid:
        for _, h in _hubs():
            aid = aid or ((h.get("projects") or {}).get(path) or {}).get("agent_id")
    return get(aid) if aid else None


def _hubs():
    return [(p, e) for p, e in registry.all_projects().items() if e.get("kind") == "weixin"]


def _set_project(proc, path, **vals):
    """改微信号 projects 里一个项目的设置（在锁里，不覆盖别的项目）。"""
    def fn(e):
        projects = e.setdefault("projects", {})
        cur = dict(projects.get(path) or {})
        cur.update(vals)
        projects[path] = {k: v for k, v in cur.items() if v}
    registry.mutate(proc, fn)


def _sync(a):
    """把 Agent 的设置写回它关联的飞书、微信登记。"""
    vals = {dst: (None if (src == "base" and a.get("base") == "claude") else a.get(src)) for src, dst in SYNCED.items()}
    for kind, proc, path in linked_paths(a["id"]):
        if kind == "feishu":
            registry.update(proc, **vals)
        else:
            _set_project(proc, path, **vals)


def mark_apply(aid, apply):
    """改模型、思考深度时选的「已开的对话也换 live / 只用于新对话 new」（#387），写在飞书、微信登记里，由它们的进程自己照办。"""
    for kind, proc, path in linked_paths(aid):
        if kind == "feishu":
            registry.update(proc, settings_apply=apply)
        else:
            _set_project(proc, path, settings_apply=apply)


def absorb(path, **changes):
    """命令行改了项目的设置（ccim config、--model、--agent）：记到对应的 Agent 上，再同步给它的其他渠道。"""
    a = for_path(path)
    if not a:
        return
    back = {src: changes[dst] for src, dst in SYNCED.items() if dst in changes}
    if "base" in back:
        back["base"] = back["base"] or "claude"
    if back:
        update(a["id"], **back)


def link(aid, kind, proc, path):
    """新接入的飞书、微信登记关联到这个 Agent，并写入它的设置。"""
    if kind == "feishu":
        registry.update(proc, agent_id=aid)
    else:
        _set_project(proc, path, agent_id=aid)
    _sync(get(aid))


# ---------- 迁移：以前的「项目」变成 Agent ----------

@contextlib.contextmanager
def _migrating():
    """迁移整个过程一把锁：几个 ccim 命令同时跑时不会给同一个目录建出两个 Agent。"""
    os.makedirs(registry.HOME, exist_ok=True)
    with open(os.path.join(registry.HOME, ".migrate.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def migrate():
    """每个接了飞书、微信的项目目录都要有 Agent：已关联的跳过；没关联的找这个目录已有的 Agent（比如网页里建的、
    或者另一个渠道已经关联的），找不到才新建。一个目录同时接了飞书和微信的，算同一个 Agent。"""
    with _migrating():
        _migrate()


def _migrate():
    data = registry.all_projects()
    paths = {}
    for p, e in data.items():
        if e.get("kind") == "platform":
            continue
        if e.get("kind") == "weixin":
            for path, s in (e.get("projects") or {}).items():
                paths.setdefault(path, {"weixin": [], "feishu": None})["weixin"].append((p, s or {}))
        else:
            paths.setdefault(p, {"weixin": [], "feishu": None})["feishu"] = e
    agents_now = _read()
    for path, ch in paths.items():
        fe = ch["feishu"] or {}
        unlinked_feishu = bool(ch["feishu"]) and not get(fe.get("agent_id") or "")
        unlinked_weixin = [hub for hub, s in ch["weixin"] if not get(s.get("agent_id") or "")]
        if not unlinked_feishu and not unlinked_weixin:
            continue
        aid = fe.get("agent_id") if get(fe.get("agent_id") or "") else None
        aid = aid or next((s.get("agent_id") for _, s in ch["weixin"] if get(s.get("agent_id") or "")), None)
        aid = aid or next((k for k, v in agents_now.items() if v.get("dir") == path), None)
        if not aid:
            ws = ch["weixin"][0][1] if ch["weixin"] else {}
            aid = create(name=os.path.basename(path) or path, base=fe.get("agent") or ws.get("agent") or "claude",
                         model=fe.get("model") or ws.get("model"), effort=fe.get("effort") or ws.get("effort"),
                         role=fe.get("role") or ws.get("role"), dir=path)["id"]
            agents_now = _read()
        if unlinked_feishu:
            link(aid, "feishu", path, path)
        for hub in unlinked_weixin:
            link(aid, "weixin", hub, path)
