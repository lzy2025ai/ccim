"""已配对项目的登记表。

~/.ccim/registry.json：{项目绝对路径: {channel, app_id, bot_name, owner_open_id, domain, group, paired_at, ...}}
~/.ccim/projects/<key>/：这个项目的运行状态（pid、日志、各聊天的会话编号）
App Secret 存进 macOS 钥匙串（服务名 ccim，账户名 app_id），不落盘。
"""
import contextlib, fcntl, hashlib, json, os, subprocess, tempfile, threading, time

HOME = os.path.expanduser("~/.ccim")
REG = os.path.join(HOME, "registry.json")
KEYCHAIN_SERVICE = "ccim"


def key(path):
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:12]


def pdir(path):
    d = os.path.join(HOME, "projects", key(path))
    if not os.path.isdir(d):                  # 生成整体状态时要调几百次：先看一眼，比每次都去建目录便宜（#264）
        os.makedirs(d, exist_ok=True)
    return d


@contextlib.contextmanager
def _locked():
    os.makedirs(HOME, exist_ok=True)
    with open(os.path.join(HOME, ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------- 一次请求里同一个文件只读一遍 ----------
# /api/state 要为每个对话、每个 Agent 查好几遍 state.json、agents.json 这些文件，每次都从头读、从头解析，
# 对话一多就要好几秒。在 snapshot() 里，文件没变（修改时间、大小、inode 都一样）就用这次请求里已经读过的。
# 读出来的东西在这次请求里共用，所以只给不改数据的接口用；要改了再写回去的地方照旧现读（_read、write_state、set_global）。

_snap = threading.local()


@contextlib.contextmanager
def snapshot():
    outer = getattr(_snap, "files", None) is not None
    if not outer:
        _snap.files = {}
    try:
        yield
    finally:
        if not outer:
            _snap.files = None


def _json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json(path):
    """读一个 JSON 文件，读不了抛 OSError / ValueError。在 snapshot() 里同一个文件没变就不重读。"""
    return load_file(path, _json)


def load_file(path, parse):
    """parse(path) 读出来的内容；在 snapshot() 里同一个文件没变就不重读（JSON 以外的配置文件也用，#264）。"""
    files = getattr(_snap, "files", None)
    if files is None:
        return parse(path)
    st = os.stat(path)
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = files.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    data = parse(path)
    files[path] = (stamp, data)
    return data


def _load(path):
    try:
        return load_json(path)
    except (OSError, ValueError):
        return {}


def _read():
    try:
        with open(REG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(HOME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".registry.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REG)


def all_projects():
    return _load(REG)


def get(path):
    return _load(REG).get(os.path.realpath(path))


def put(path, entry):
    with _locked():
        data = _read()
        data[os.path.realpath(path)] = entry
        _write(data)


def update(path, **changes):
    with _locked():
        data = _read()
        p = os.path.realpath(path)
        if p not in data:
            raise KeyError(path)
        data[p].update(changes)
        _write(data)
        return data[p]


def mutate(path, fn):
    """在锁里改一条登记：fn(条目) 原地修改。几个进程同时改同一个微信号的 projects 时不会互相覆盖。"""
    with _locked():
        data = _read()
        p = os.path.realpath(path)
        if p not in data:
            raise KeyError(path)
        fn(data[p])
        _write(data)
        return data[p]


def remove(path):
    with _locked():
        data = _read()
        entry = data.pop(os.path.realpath(path), None)
        _write(data)
        return entry


def find(name_or_path):
    """按路径、项目名（目录名）或机器人名找已配对的项目，返回 (路径, 条目)。"""
    data = _read()
    if name_or_path is None:
        p = os.path.realpath(os.getcwd())
        return (p, data[p]) if p in data else (p, None)
    p = os.path.realpath(os.path.expanduser(name_or_path))
    if p in data:
        return p, data[p]
    hits = [(k, v) for k, v in data.items() if os.path.basename(k) == name_or_path and v.get("kind") != "weixin"]
    if not hits:
        hits = [(k, v) for k, v in data.items() if v.get("bot_name") == name_or_path]
    if not hits and name_or_path in ("智能体平台", "平台", "网页", "web"):
        hits = [(k, v) for k, v in data.items() if v.get("kind") == "platform"]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f"有 {len(hits)} 个项目都叫 {name_or_path}，请写完整路径：\n" + "\n".join(k for k, _ in hits))
    return p, None


# ---------- 钥匙串 ----------

def set_secret(app_id, secret):
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", app_id, "-w", secret],
                   check=True, capture_output=True)


def get_secret(app_id):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", app_id, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"钥匙串里找不到 {app_id} 的密钥，请重新配对：ccim unpair 后再运行 ccim")
    return r.stdout.strip()


def del_secret(app_id):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", app_id], capture_output=True)


# ---------- 每个项目的运行状态 ----------

def state_path(path):
    return os.path.join(pdir(path), "state.json")


def read_state(path):
    return _load(state_path(path))


def write_state(path, **changes):
    try:
        with open(state_path(path), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.update(changes)
    fd, tmp = tempfile.mkstemp(dir=pdir(path), prefix=".state.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path(path))
    return s


def global_config():
    """ccim config --global 设的全局默认：model / effort 给 Claude Code，codex_model / codex_effort 给 Codex；
    Agent（项目）自己设了的以它为准。"""
    return _load(os.path.join(HOME, "config.json"))


def set_global(**changes):
    with _locked():
        try:
            with open(os.path.join(HOME, "config.json"), encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            c = {}
        c.update(changes)
        c = {k: v for k, v in c.items() if v}
        fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".config.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(HOME, "config.json"))
        return c


def all_chats(path):
    """这个项目在各个渠道里的对话：[(在哪, 地址, 记录)]。飞书的记在项目自己的状态里，
    微信的记在各个微信号的状态里（一个微信号管好几个项目）。"""
    path = os.path.realpath(path)
    entry = get(path) or {}
    where = {"feishu": "飞书"}.get(entry.get("channel"), "飞书")
    out = [(where, k, c) for k, c in (read_state(path).get("chats") or {}).items()]
    for hub, e in all_projects().items():
        if e.get("kind") == "weixin" and path in (e.get("projects") or {}):
            chats = (read_state(hub).get("chats") or {}).get(path) or {}
            where = "微信" if e.get("bot_name") == "微信" else f"微信「{e.get('bot_name')}」"
            out += [(where, k, c) for k, c in chats.items()]
    return out


# ---------- Claude Code 自己的设置 ----------

ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001",
           "fable": "claude-fable-5-1"}


def claude_defaults(path):
    """没在 ccim 里单独设置时，Claude Code 会用的模型和思考深度：项目本地设置 > 项目设置 > 全局设置。
    ccim 不传这两个参数，交给 Claude Code 自己按这个顺序取；这里只是读出来给 /status、ccim show 显示。"""
    model = effort = None
    per_model = {}
    for f in (os.path.join(path, ".claude/settings.local.json"), os.path.join(path, ".claude/settings.json"),
              os.path.expanduser("~/.claude/settings.json")):
        try:
            s = load_json(f)
        except (OSError, ValueError):
            continue
        model = model or s.get("model")
        effort = effort or s.get("effortLevel")
        for k, v in (s.get("modelSettings") or {}).items():
            per_model.setdefault(k, (v or {}).get("effortLevel"))
    return model, effort, per_model


def effective(path, model=None, effort=None):
    """实际在用的 (模型, 思考深度, 模型是否单独设置, 思考深度是否单独设置)。"""
    d_model, d_effort, per_model = claude_defaults(path)
    m = model or d_model or "默认"
    base = (m.split("[")[0] if m else "")
    e = effort or per_model.get(ALIASES.get(base, base)) or d_effort or "默认"
    return m, e, bool(model), bool(effort)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
