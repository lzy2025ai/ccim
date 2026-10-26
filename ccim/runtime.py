"""稳定版、开发版，安全重启，验收后升级。

- 开发版：ccim 代码目录（git 仓库）的 .venv，改了代码马上生效。只给 ccim 自己的测试机器人用。
- 稳定版：~/.ccim/stable/<版本>/，用 `ccim promote` 从某次提交装出来的独立环境，日常项目都跑它。
  ~/.ccim/stable/channel.json 记着 current（当前稳定版）、previous（上一个）、dev_python（开发版的 python）。
- 每个项目跑哪个：登记表里的 runtime（dev / stable，不写就是有稳定版用稳定版）；stable_version 可以钉在某个版本上。

安全重启（`ccim restart --safe`、飞书里的 /restart）：由独立进程执行，所以重启的是自己也不怕；
新版本 60 秒内没连上，就改用稳定版启动，保证还能从飞书联系上；结果都发飞书告诉主人。
这个独立进程优先用稳定版跑，新代码坏了也不影响它。
"""
import json, os, re, shutil, subprocess, sys, time

from . import registry

def _stable():
    return os.path.join(registry.HOME, "stable")


def _channel_file():
    return os.path.join(_stable(), "channel.json")


def _rm(p):
    """只删稳定版目录里的东西。"""
    root = os.path.realpath(_stable())
    real = os.path.realpath(p)
    if not real.startswith(root + os.sep) or real == root:
        raise RuntimeError(f"拒绝删除 {p}")
    shutil.rmtree(real, ignore_errors=True)
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 开发版时就是代码目录


def _channel():
    try:
        with open(_channel_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_channel(**changes):
    c = _channel()
    c.update(changes)
    os.makedirs(_stable(), exist_ok=True)
    with open(_channel_file() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)
    os.replace(_channel_file() + ".tmp", _channel_file())


def _is_dev_install():
    return os.path.exists(os.path.join(SRC, ".git"))       # 工作目录（git worktree）里的 .git 是文件


def _in_stable(python):
    return _env(python).startswith(os.path.realpath(_stable()) + os.sep)


def dev_python():
    # 稳定版的 Python 带着 PYTHONPATH 导入了代码目录里的代码时，_is_dev_install 也是真，但它不是开发版（#111）
    if _is_dev_install() and not _in_stable(sys.executable):
        return sys.executable
    p = _channel().get("dev_python")
    return p if p and os.path.exists(p) else None


def stable_python(version=None):
    v = version or _channel().get("current")
    p = os.path.join(_stable(), v, "bin", "python") if v else None
    return p if p and os.path.exists(p) else None


def which(path, entry=None, force=None):
    """这个项目该用哪个：返回 (python, 说明)。"""
    entry = entry if entry is not None else (registry.get(path) or {})
    rt = force or entry.get("runtime") or ("stable" if stable_python() else "dev")
    if rt == "stable":
        v = entry.get("stable_version") or _channel().get("current")
        p = stable_python(v)
        need = {"weixin": "weixin_hub.py", "platform": "platform.py"}.get(entry.get("kind"))   # 老稳定版没有的进程跑开发版
        if p and (not need or _has(p, need)):
            return p, f"稳定版 {v}"
    p = dev_python() or sys.executable
    return p, "开发版"


def _has(python, module):
    import glob
    return bool(glob.glob(os.path.join(os.path.dirname(os.path.dirname(python)), "lib", "python*", "site-packages", "ccim",
                                       module)))


def _env(python):
    """python 属于哪个环境（目录）。venv 里的 python 是指向系统 python 的软链，不能直接 realpath 比较。"""
    return os.path.realpath(os.path.dirname(python))


DESCRIBE_TTL = 10
_described = {}                               # python → (算的时刻, 结果)：开发版要问 git，网页每次刷新都要列一遍


def describe(python):
    """正在运行的那个 python 是哪个版本，给 ccim show、list 用。"""
    if not python:
        return ""
    hit = _described.get(python)
    if hit and time.time() - hit[0] < DESCRIBE_TTL:
        return hit[1]
    v = _describe(python)
    _described[python] = (time.time(), v)
    return v


def _describe(python):
    real = _env(python)
    if _in_stable(python):
        return "稳定版 " + os.path.relpath(real, os.path.realpath(_stable())).split(os.sep)[0]
    t = trying()
    if t:                                     # 试用中：开发的人看，写需求编号
        return f"开发版（在试 {t['label']}）"
    v = code_version(_repo())
    return f"开发版 {v}" if v else "开发版"


def code_version(src):
    """代码目录的版本号：pyproject 里的版本 + 提交，和稳定版的写法一样（0.19.0-6f11bf9）。"""
    try:
        with open(os.path.join(src, "pyproject.toml"), encoding="utf-8") as f:
            ver = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
    except (OSError, AttributeError):
        return ""
    r = _run(["git", "-C", src, "rev-parse", "--short=7", "HEAD"], timeout=10)
    return f"{ver}-{r.stdout.strip()}" if r.returncode == 0 and r.stdout.strip() else ver


# ---------- 验收时试某个需求（#42） ----------
# 每个需求在自己的工作目录（git worktree）里开发，主目录不切分支。`ccim try 编号` 把跑开发版的进程切到那个工作目录上：
# 启动时把它放在 PYTHONPATH 最前面，导入的就是那里的代码（开发环境还是主目录的 .venv，依赖一样）。

def trying():
    """正在试的需求：{label, src, branch}，没有返回 None。"""
    t = _channel().get("try")
    return t if t and os.path.isdir(t.get("src") or "") else None


def launch_env(python):
    """启动这个 python 时额外的环境变量：开发版且正在试某个需求时指向那个工作目录。"""
    t = trying()
    if not t or not python or _in_stable(python):
        return {}
    return {"PYTHONPATH": t["src"]}


def worktrees():
    """[(目录, 分支)]"""
    r = _git("worktree", "list", "--porcelain")
    out, cur = [], {}
    for line in r.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("worktree"):
                out.append((cur["worktree"], cur.get("branch", "").replace("refs/heads/", "")))
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    return out


def find_worktree(what):
    """按需求编号、分支名或目录找工作目录。"""
    what = str(what).lstrip("#")
    for path, branch in worktrees()[1:]:      # 第一个是主目录
        if (what.isdigit() and re.match(rf"issue-{what}(-|$)", branch)) or branch == what or \
                os.path.realpath(path) == os.path.realpath(os.path.expanduser(what)):
            return path, branch
    return None


def dev_projects():
    """跑开发版的项目（试需求时要一起切换的）。"""
    return [p for p, e in registry.all_projects().items() if which(p, e)[1] == "开发版"]


def set_try(what, caller=None, label=None):
    """what 为 main / None 就切回主目录。返回 [说明]。
    caller：{process, chat}，由平台代为执行时（ccim ship，#282）发起的对话；label：重启后说明里的说法。"""
    if not _is_dev_install():
        raise SystemExit("要在 ccim 的代码目录（开发版）里运行。")
    text = label
    if what in (None, "", "main"):
        _save_channel(**{"try": None})
        label = "main"
    else:
        hit = find_worktree(what)
        if not hit:
            raise SystemExit(f"找不到 {what} 的工作目录。先让 Agent 建好（git worktree add …），或者写分支名、目录。")
        path, branch = hit
        m = re.match(r"issue-(\d+)", branch)
        label = f"#{m.group(1)}" if m else branch
        _save_channel(**{"try": {"label": label, "src": path, "branch": branch}})
    # 一律交给独立进程重启（#70）：在平台里的 Agent 执行这条命令时，停平台会连带结束这条命令，在这里重启就没人再把它启动了
    from . import daemon
    paths = [p for p in dev_projects() if daemon.running(p)]
    if not paths:
        return label, ["现在没有跑开发版的项目，下次启动时生效。"]
    inner = bool(os.environ.get("CCIM_PROCESS"))
    base = 15 if inner else 1                 # 在 ccim 里调用：留时间把回复发出去（重启前还会等这一轮答完，#136）
    started = registry.now()
    paths.sort(key=lambda p: (registry.get(p) or {}).get("kind") == "platform")   # 网页平台最后
    report = {"CCIM_RESTART_LABEL": text or (f"切到 {label}" if label != "main" else f"切回正式版本 {code_version(_repo())}".strip()),
              "CCIM_RESTART_PATHS": json.dumps(paths),
              "CCIM_RESTART_SINCE": started}   # 以这一刻为准判断各个进程重启过没有（#140：先重启好的不再误报没起来）
    if caller:                                # 重启前等这个对话答完，重启后在这个对话里说结果（_before_restart 认这两个）
        report.update(CCIM_PROCESS=caller["process"], CCIM_CHAT=caller["chat"])
    for i, p in enumerate(paths):
        schedule_restart(p, delay=base + 3 * i, notify=False, env=report)   # 结果由执行 ccim try 的人看，不再推到飞书、微信
    names = "、".join(proc_label(p) for p in paths)
    if inner:
        return label, [f"{names} 会在这一轮回复发出去、手头的活做完后依次重启（最多等 {WAIT_IDLE // 60} 分钟），"
                       "重启好了在这里说一声；新版本起不来会用稳定版顶上。"]
    out, left = [], list(paths)
    for _ in range(120):                      # 在终端里调用：等它们都重新连上再告诉结果
        time.sleep(1)
        for p in list(left):
            st = registry.read_state(p)
            if st.get("started", "") >= started and st.get("online") and daemon.running(p):
                out.append(f"{proc_label(p)}已重启，运行的是{describe(st.get('python'))}{'（顶替中）' if st.get('fallback') else ''}。")
                left.remove(p)
        if not left:
            break
    out += [f"{proc_label(p)}还没起来，用 ccim show {os.path.basename(p)} 看看。" for p in left]
    return label, out


def proc_label(path):
    """给人看的进程名（#140）：网页平台、飞书机器人「名字」、微信「名字」。"""
    from .channels import NAMES
    e = registry.get(path) or {}
    name = e.get("bot_name") or os.path.basename(path)
    if e.get("kind") == "platform":
        return "网页平台"
    if e.get("kind") == "weixin":
        return "微信" if name == "微信" else f"微信「{name}」"
    return f"{NAMES.get(e.get('channel'), '飞书')}机器人「{name}」"


def inside(path):
    """这条命令是不是在 path 这个 ccim 进程里面跑的（比如平台里的 Agent 执行的）。"""
    return os.environ.get("CCIM_PROCESS") == os.path.realpath(path)


# ---------- 安全重启 ----------

WAIT_IDLE = 180                               # 重启前最多等手头的对话做完这么久（#136）


def schedule_restart(path, delay=15, target=None, notify=True, env=None):
    """另起一个脱离当前进程的进程去重启，立即返回。target：dev / stable / None（按项目设置）。
    notify=False：不在飞书、微信里通知结果（用环境变量传，跑重启的稳定版旧了也不会因为不认识参数而失败）。"""
    py = stable_python() or dev_python() or sys.executable
    args = [py, "-P", "-m", "ccim.cli", "safe-restart-run", path, "--delay", str(delay)]
    if target:
        args += ["--target", target]
    log = open(os.path.join(registry.pdir(path), "restart.log"), "ab")
    env = dict(os.environ, **({} if notify else {"CCIM_RESTART_QUIET": "1"}), **(env or {}))
    env.pop("PYTHONPATH", None)               # 在试用某个需求的进程里调用时带着那个工作目录；要试哪个由 launch_env 决定（#111）
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd="/", env=env)


def safe_restart(path, delay=0, target=None, notify=True):
    """在独立进程里执行。返回 (成功, 说明)。"""
    from . import daemon
    time.sleep(delay)
    entry = registry.get(path)
    name = proc_label(path)
    r = daemon.running(path)
    if r and r[1] == "fg":
        msg = f"{name}正在终端前台运行，没法自动重启。在那个终端按 Ctrl+C，再运行 ccim start --always。"
        _tell(path, entry, msg, notify)
        return False, msg
    always = daemon.is_always(path) or not r
    py, desc = which(path, entry, target)
    if r:
        _before_restart(path)
    daemon.stop(path)
    try:
        start = daemon.start_always if always else daemon.start_background
        start(path, entry["channel"], python=py)
        time.sleep(8)                         # 连上之后再看一会儿，确认不是马上就崩
        if not daemon.running(path):
            raise SystemExit("启动后很快退出了：" + daemon._last_line(daemon.log_path(path)))
        registry.write_state(path, fallback=None)
        msg = f"{name}已重启，运行的是{desc}。"
        _tell(path, entry, msg, notify)
        return True, msg
    except SystemExit as e:
        err = str(e).split("\n")[0]
        fallback = stable_python(entry.get("stable_version")) or stable_python()
        if not fallback or _env(fallback) == _env(py):
            msg = f"{name}重启失败，也没有别的版本可以顶上：{err}"
            _tell(path, entry, msg, notify)
            return False, msg
        daemon.stop(path)
        try:
            (daemon.start_always if always else daemon.start_background)(path, entry["channel"], python=fallback)
        except SystemExit as e2:
            msg = f"{name}新版本没起来，稳定版也没起来：{err}；{str(e2).splitlines()[0]}"
            _tell(path, entry, msg, notify)
            return False, msg
        registry.write_state(path, fallback=True)
        msg = f"{name}{desc}没起来，先用{describe(fallback)} 顶上了。\n报错：{err}"
        _tell(path, entry, msg, notify)
        return False, msg


def _tell(path, entry, text, notify):
    print(text, flush=True)
    if not notify or not entry or not entry.get("owner_open_id"):
        return
    import asyncio
    from .channels import NAMES, send_once
    try:
        asyncio.run(send_once(path, entry, text))
    except BaseException as e:                # get_secret 失败是 SystemExit
        print(f"（没能在{NAMES.get(entry.get('channel'), '飞书')}里通知：{e}）", flush=True)


# ---------- 验收后升级 ----------

def _git(*args):
    return _run(["git", "-C", SRC, *args], timeout=60)


TIMEOUT_RC = -999                             # _run 超时时的返回码
INSTALL_TIMEOUT = 600
LOOSE = [False]                               # 这次装的时候依赖没能沿用原来的版本，按最新的装了


def _run(cmd, env=None, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, TIMEOUT_RC, "", "")


def _pins():
    """当前稳定版装的各依赖版本，写成 uv 的约束文件，返回路径；没有就 None。"""
    py = stable_python()
    if not py:
        return None
    r = _run(["uv", "pip", "freeze", "--python", py], timeout=60)
    lines = [l for l in r.stdout.splitlines() if "==" in l and not l.lower().startswith("ccim")]
    if r.returncode != 0 or not lines:
        return None
    import tempfile
    fd, path = tempfile.mkstemp(prefix="ccim-pins-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _repo():
    """主目录：在试某个需求时代码在它的工作目录里，从那里装也一律装主仓库里的提交。"""
    r = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    d = r.stdout.strip()
    return os.path.dirname(d) if r.returncode == 0 and d.endswith("/.git") else SRC


def build_stable():
    """把代码目录当前的提交装成一个稳定版，返回版本名。"""
    if not _is_dev_install():
        raise SystemExit("要在 ccim 的代码目录（开发版）里运行。")
    # 一律按最新的 main 装（#42）：主目录可能正停在别的分支上，或者有别人没提交的改动
    if _git("fetch", "-q", "origin", "main").returncode == TIMEOUT_RC:
        print("取最新的 main 超时了，用本地已有的。", flush=True)
    ref = "origin/main" if _git("rev-parse", "--verify", "-q", "origin/main").returncode == 0 else "main"
    sha = _git("rev-parse", ref).stdout.strip()
    _git("branch", "-f", "ccim-promote", sha)   # uv 从本地仓库取代码时只认分支上的提交，origin/main 不在任何本地分支上
    import tomllib                            # 版本号读那次提交里的 pyproject
    ver = tomllib.loads(_git("show", f"{sha}:pyproject.toml").stdout)["project"]["version"]
    label = f"{ver}-{sha[:7]}"
    dest = os.path.join(_stable(), label)
    if not stable_python(label):
        _rm(dest)
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        r = _run(["uv", "venv", "-q", "--python", sys.executable, dest], env, 120)
        if r.returncode == 0:
            # 依赖沿用当前稳定版装好的版本（#146）：不然每次都去拉最新的，大的依赖包在网络慢时要下很久；对不上再放开
            pins = _pins()
            install = ["uv", "pip", "install", "-q", "--compile-bytecode", "--python", os.path.join(dest, "bin", "python"),
                       f"git+file://{_repo()}@{sha}"]
            r = _run(install + (["-c", pins] if pins else []), env, INSTALL_TIMEOUT)
            if r.returncode not in (0, TIMEOUT_RC) and pins:
                r = _run(install, env, INSTALL_TIMEOUT)
                LOOSE[0] = r.returncode == 0
            if pins:
                os.unlink(pins)
        if r.returncode != 0:
            _rm(dest)
            if r.returncode == TIMEOUT_RC:
                raise SystemExit(f"装稳定版超时（{INSTALL_TIMEOUT // 60} 分钟没装完），多半是网络慢，下载依赖包卡住了。稍后再试。")
            raise SystemExit(f"装稳定版失败：{(r.stderr or r.stdout).strip()[-500:]}")
    r = subprocess.run([os.path.join(dest, "bin", "python"), "-P", "-c", "import ccim.cli, ccim.channels.feishu, ccim.channels.weixin, ccim.weixin_hub"],
                       capture_output=True, text=True, cwd="/")
    if r.returncode != 0:
        raise SystemExit(f"新稳定版有问题，没有启用：{r.stderr.strip()[-500:]}")
    return label


def promote(wait=180):
    """装出新稳定版，把跑稳定版的项目逐个安全重启；哪个起不来，就把它钉回上一个稳定版。"""
    from . import daemon
    label = build_stable()
    old = _channel().get("current")
    if old == label:
        print(f"稳定版已经是 {label}。")
    else:
        _save_channel(current=label, previous=old, dev_python=sys.executable)
        print(f"稳定版：{old or '无'} → {label}")
    results = []
    for path, e in sorted(registry.all_projects().items()):
        name = proc_label(path)
        if which(path, e)[1] == "开发版":
            continue
        pinned = e.get("stable_version")
        if pinned:
            registry.update(path, stable_version=None)
        r = daemon.running(path)
        if not r:
            results.append(f"{name}没在运行，下次启动就用新版本")
            continue
        now = registry.read_state(path).get("python")
        if not pinned and now and _env(now) == _env(stable_python(label)):
            results.append(f"{name}已经是新版本，没有重启")
            continue
        if r[1] == "fg":
            results.append(f"{name}在终端前台运行，要到那个终端里手动重启")
            continue
        _wait_idle(path, wait)
        ok, _ = safe_restart(path, notify=False)
        if ok:
            results.append(f"{name}已升级到 {label}")
            continue
        if old:
            registry.update(path, stable_version=old)
            safe_restart(path, notify=False)
        results.append(f"{name}新版本起不来，已退回 {old or '原来的版本'}，日志：{daemon.log_path(path)}")
    _prune()
    return label, results


# ---------- 在对话里升级：先确认，后台做，做完回报（#146） ----------

# 改了也不用重启的（#228）：文档、仓库说明、插件里的 skill 和钩子、CI 和部署脚本。
# 网页和随包图标每次请求现读，只对直接从代码目录跑的进程（开发版）不用重启
NO_RESTART = ("docs/", ".github/", "deploy/", "skills/", "hooks/", ".claude-plugin/", "tests/", "LICENSE")
LIVE = ("ccim/web.html", "ccim/static/")


def needs_restart(a, b, live=False):
    """代码从提交 a 换到 b，在跑的进程要不要重启。比不出来（没有 a、提交找不到）算要。"""
    if not a or not b:
        return True
    if all(re.fullmatch(r"[0-9a-f]{7,40}", x) for x in (a, b)) and (b.startswith(a) or a.startswith(b)):
        return False                          # 同一个提交（一个是缩写）
    r = _git("diff", "--name-only", "-z", a, b)      # -z：中文文件名不加引号转义
    if r.returncode != 0:
        return True
    free = NO_RESTART + (LIVE if live else ())
    return any(not (f.endswith(".md") or f.startswith(free)) for f in r.stdout.split("\0") if f)


def _stale_stable(py=None):
    """跑稳定版、但不是当前稳定版的进程（钉了版本的也算）。"""
    from . import daemon
    py = py or stable_python()
    return [proc_label(p) for p, e in sorted(registry.all_projects().items())
            if which(p, e)[1] != "开发版" and daemon.running(p)
            and (e.get("stable_version") or not py or _env(registry.read_state(p).get("python") or "") != _env(py))]


def stable_behind(new=None):
    """要不要打新的稳定版、重启跑稳定版的进程：main 上有要重启的改动，或者还有进程在跑旧的稳定版。"""
    cur = (_channel().get("current") or "").rsplit("-", 1)[-1]
    new = new or _git("rev-parse", "origin/main").stdout.strip()
    web = any(e.get("kind") == "platform" and which(p, e)[1] != "开发版" for p, e in registry.all_projects().items())
    return needs_restart(cur, new, live=not web) or bool(_stale_stable())   # 跑稳定版的里没有网页平台：网页的改动用不上


def promote_plan():
    """确认卡片上写的：升级的是什么、这次改了什么、会重启哪些。不用升级（已是最新、也没有要重启的）返回 None。"""
    from . import daemon
    _git("fetch", "-q", "origin", "main")
    cur = (_channel().get("current") or "").rsplit("-", 1)[-1]
    ref = "origin/main" if _git("rev-parse", "--verify", "-q", "origin/main").returncode == 0 else "main"
    new = _git("rev-parse", ref).stdout.strip()
    stale = _stale_stable()
    same = bool(cur) and new.startswith(cur)
    if same and not stale:
        return None
    changes = []
    if cur and not same:
        changes = [_change(l) for l in _git("log", "--format=%s", f"{cur}..{new}").stdout.splitlines()]
    names = stale if same else [proc_label(p) for p, e in sorted(registry.all_projects().items())
                                if which(p, e)[1] != "开发版" and daemon.running(p)]
    parts = ["把 ccim（在网页、飞书、微信里和 Agent 聊天的这套程序）升级到最新版本。"
             if not same else "ccim 已经是最新版本，把还在跑旧版本的换上去。"]
    if changes:
        more = f"\n- 另外还有 {len(changes) - 8} 项" if len(changes) > 8 else ""
        parts.append("这次的改动：\n" + "\n".join(f"- {c}" for c in changes[:8]) + more)
    parts.append(f"升级后重启：{'、'.join(names)}（各自等手头的活做完再重启）。" if names else "现在没有要重启的。")
    parts.append("Claude Code、Claude SDK 等依赖沿用现在的版本，不跟着升级。在后台进行，这边接着做别的，做完在这个对话里说结果。")
    return "\n\n".join(parts)


def _change(line):
    """提交说明给人看的一句：冒号后面多是怎么改的，只留前面做了什么；前面太短（「开发流程：…」）就留整句。"""
    line = re.sub(r"\s*\(?#\d+\)?", "", line).strip()
    head = line.split("：")[0].strip()
    return head if len(head) >= 8 else line


def start_promote():
    """在 ccim 的对话里运行 ccim promote：交给独立进程，马上返回。装稳定版要用开发版的代码目录。"""
    py = dev_python() or sys.executable
    os.makedirs(_stable(), exist_ok=True)
    log = open(os.path.join(_stable(), "promote.log"), "ab")
    subprocess.Popen([py, "-P", "-m", "ccim.cli", "promote-run"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, cwd="/")


def promote_run():
    """独立进程里：升级，把结果交回发起的对话。"""
    import fcntl
    proc, chat = os.environ.pop("CCIM_PROCESS", None), os.environ.pop("CCIM_CHAT", None)   # 下面的重启不算这个对话发起的
    os.makedirs(_stable(), exist_ok=True)
    lock = open(os.path.join(_stable(), ".promote.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        text = "已经有一次升级在进行，这次没有再升级；那一次做完会说结果。"
    else:
        try:
            label, results = promote()
            text = (f"ccim 升级好了（{label}）：\n" + "\n".join(f"- {r}" for r in results or ["没有要重启的。"]) +
                    ("\n- 依赖没法沿用原来的版本，按新版本要求装了新的" if LOOSE[0] else ""))
        except SystemExit as e:
            text = f"ccim 升级没成功：{e}"
        except Exception as e:
            text = f"ccim 升级出错了：{e}"
    print(registry.now(), text, flush=True)
    try:
        with open(os.path.join(_stable(), "promote.json"), "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "text": text, "ok": text.startswith("ccim 升级好了")}, f, ensure_ascii=False)
    except OSError:
        pass
    if proc and chat:
        post_note(proc, chat, text)


def promoting():
    """后台升级（promote-run）还在做：它一直拿着 .promote.lock。"""
    import fcntl
    try:
        with open(os.path.join(_stable(), ".promote.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def last_promote():
    """上一次在后台升级的结果（开发页显示）。"""
    try:
        with open(os.path.join(_stable(), "promote.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _notes(path):
    d = os.path.join(registry.pdir(path), "notes")
    os.makedirs(d, exist_ok=True)
    return d


def post_note(path, chat, text):
    """让 path 这个 ccim 进程往 chat 里发一句话（它每秒取一次；没在运行就等下次启动）。"""
    import uuid
    f = os.path.join(_notes(path), uuid.uuid4().hex[:10] + ".json")
    with open(f + ".tmp", "w", encoding="utf-8") as fp:
        json.dump({"chat": chat, "text": text, "at": registry.now()}, fp, ensure_ascii=False)
    os.replace(f + ".tmp", f)


def take_notes(path):
    out = []
    d = os.path.join(registry.pdir(path), "notes")
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".json"):
            continue
        f = os.path.join(d, name)
        try:
            with open(f, encoding="utf-8") as fp:
                n = json.load(fp)
            out.append((n["chat"], n["text"]))
        except (OSError, ValueError, KeyError):
            pass
        try:
            os.unlink(f)
        except OSError:
            pass
    return out


def _wait_idle(path, limit):
    """等这个项目手头的对话做完（最多 limit 秒）。返回到时候还没做完的对话。"""
    deadline = time.monotonic() + limit
    while True:
        busy = [cid for cid, c in (registry.read_state(path).get("chats") or {}).items() if c.get("busy")]
        if not busy or time.monotonic() >= deadline:
            return busy
        time.sleep(3)


def _before_restart(path):
    """重启前（#136）：等发起重启的那一轮回复发出去、别的对话这一轮做完（最多 WAIT_IDLE 秒）；
    还没做完的记下来，重启后在那些对话里说明；在这个进程里的对话发起的，记下来重启好了回它一句。"""
    caller = os.environ.get("CCIM_CHAT") if os.environ.get("CCIM_PROCESS") == os.path.realpath(path) else None
    left = _wait_idle(path, WAIT_IDLE)
    changes = {"restart_cut": {"at": registry.now(), "chats": left} if left else None}
    if caller:
        try:
            paths = json.loads(os.environ.get("CCIM_RESTART_PATHS") or "[]") or [path]
        except ValueError:
            paths = [path]
        changes["restart_report"] = {"chat": caller, "since": os.environ.get("CCIM_RESTART_SINCE") or registry.now(), "paths": paths,
                                     "label": os.environ.get("CCIM_RESTART_LABEL") or "重启"}
    registry.write_state(path, **changes)


def after_restart_note(path):
    """重启后的新进程调用：[(对话, 要说的话)]。发起重启的那个对话：等各个进程都起来（最多 2 分钟）再说结果。"""
    st = registry.read_state(path)
    cut, rep = st.get("restart_cut") or {}, st.get("restart_report")
    if not cut and not rep:
        return []
    registry.write_state(path, restart_cut=None, restart_report=None)
    out = [(cid, "上一轮被 ccim 重启打断了，没做完。要接着做就跟它说一声。") for cid in cut.get("chats") or []
           if not rep or cid != rep.get("chat")]
    if rep:
        from . import daemon
        deadline = time.monotonic() + 120
        while True:
            rows = [(p, registry.read_state(p)) for p in rep.get("paths") or [path]]
            up = [(p, s) for p, s in rows if (s.get("started") or "") >= rep["since"] and s.get("online") and daemon.running(p)]
            if len(up) == len(rows) or time.monotonic() >= deadline:
                break
            time.sleep(3)
        ok = {p for p, _ in up}
        lines = [f"- {proc_label(p)}" + (f"在线，运行的是{describe(s.get('python'))}{'（新版本没起来，先用它顶上）' if s.get('fallback') else ''}"
                                                if p in ok else "还没起来，用 ccim show 看看") for p, s in rows]
        out.append((rep["chat"], f"已{rep.get('label') or '重启'}：\n" + "\n".join(lines)))
    return out


def _prune():
    """只留当前、上一个、和被项目钉住的稳定版。"""
    c = _channel()
    keep = {c.get("current"), c.get("previous")} | {e.get("stable_version") for e in registry.all_projects().values()}
    for d in os.listdir(_stable()) if os.path.isdir(_stable()) else []:
        if d not in keep and os.path.isdir(os.path.join(_stable(), d)):
            _rm(os.path.join(_stable(), d))
