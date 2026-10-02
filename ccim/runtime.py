"""稳定版、开发版，安全重启，验收后升级。

- 开发版：ccim 代码目录（git 仓库）的 .venv，改了代码马上生效。只给 ccim 自己的测试机器人用。
- 稳定版：~/.ccim/stable/<版本>/，用 `ccim promote` 从某次提交装出来的独立环境，日常项目都跑它。
  ~/.ccim/stable/channel.json 记着 current（当前稳定版）、previous（上一个）、dev_python（开发版的 python）。
- 每个项目跑哪个：登记表里的 runtime（dev / stable，不写就是有稳定版用稳定版）；stable_version 可以钉在某个版本上。

安全重启（`ccim restart --safe`、飞书里的 /restart）：由独立进程执行，所以重启的是自己也不怕；
新版本 60 秒内没连上，就改用稳定版启动，保证还能从飞书联系上；结果都发飞书告诉主人。
这个独立进程优先用稳定版跑，新代码坏了也不影响它。
"""
import json, os, shutil, subprocess, sys, time

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
    return os.path.isdir(os.path.join(SRC, ".git"))


def dev_python():
    if _is_dev_install():
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
        if p:
            return p, f"稳定版 {v}"
    p = dev_python() or sys.executable
    return p, "开发版"


def describe(python):
    """正在运行的那个 python 是哪个版本，给 ccim show、list 用。"""
    if not python:
        return ""
    real = os.path.realpath(python)
    if real.startswith(os.path.realpath(_stable()) + os.sep):
        return "稳定版 " + os.path.relpath(real, os.path.realpath(_stable())).split(os.sep)[0]
    return "开发版"


# ---------- 安全重启 ----------

def schedule_restart(path, delay=15, target=None):
    """另起一个脱离当前进程的进程去重启，立即返回。target：dev / stable / None（按项目设置）。"""
    py = stable_python() or dev_python() or sys.executable
    args = [py, "-P", "-m", "ccim.cli", "safe-restart-run", path, "--delay", str(delay)]
    if target:
        args += ["--target", target]
    log = open(os.path.join(registry.pdir(path), "restart.log"), "ab")
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd="/")


def safe_restart(path, delay=0, target=None, notify=True):
    """在独立进程里执行。返回 (成功, 说明)。"""
    from . import daemon
    time.sleep(delay)
    entry = registry.get(path)
    name = os.path.basename(path)
    r = daemon.running(path)
    if r and r[1] == "fg":
        msg = f"「{name}」正在终端前台运行，没法自动重启。在那个终端按 Ctrl+C，再运行 ccim start --always。"
        _tell(path, entry, msg, notify)
        return False, msg
    always = daemon.is_always(path) or not r
    py, desc = which(path, entry, target)
    daemon.stop(path)
    try:
        start = daemon.start_always if always else daemon.start_background
        start(path, entry["channel"], python=py)
        time.sleep(8)                         # 连上之后再看一会儿，确认不是马上就崩
        if not daemon.running(path):
            raise SystemExit("启动后很快退出了：" + daemon._last_line(daemon.log_path(path)))
        registry.write_state(path, fallback=None)
        msg = f"「{name}」已重启，运行的是{desc}。"
        _tell(path, entry, msg, notify)
        return True, msg
    except SystemExit as e:
        err = str(e).split("\n")[0]
        fallback = stable_python(entry.get("stable_version")) or stable_python()
        if not fallback or os.path.realpath(fallback) == os.path.realpath(py):
            msg = f"「{name}」重启失败，也没有别的版本可以顶上：{err}"
            _tell(path, entry, msg, notify)
            return False, msg
        daemon.stop(path)
        try:
            (daemon.start_always if always else daemon.start_background)(path, entry["channel"], python=fallback)
        except SystemExit as e2:
            msg = f"「{name}」新版本没起来，稳定版也没起来：{err}；{str(e2).splitlines()[0]}"
            _tell(path, entry, msg, notify)
            return False, msg
        registry.write_state(path, fallback=True)
        msg = f"「{name}」{desc}没起来，先用{describe(fallback)}顶上了。\n报错：{err}"
        _tell(path, entry, msg, notify)
        return False, msg


def _tell(path, entry, text, notify):
    print(text, flush=True)
    if not notify or not entry or not entry.get("owner_open_id"):
        return
    import asyncio
    from .channels.feishu import Feishu
    try:
        ch = Feishu(path, entry, registry.get_secret(entry["app_id"]), {})
        asyncio.run(ch.send_to_user(entry["owner_open_id"], text))
    except BaseException as e:                # get_secret 失败是 SystemExit
        print(f"（没能在飞书里通知：{e}）", flush=True)


# ---------- 验收后升级 ----------

def _git(*args):
    return subprocess.run(["git", "-C", SRC, *args], capture_output=True, text=True)


def build_stable():
    """把代码目录当前的提交装成一个稳定版，返回版本名。"""
    if not _is_dev_install():
        raise SystemExit("要在 ccim 的代码目录（开发版）里运行。")
    if _git("status", "--porcelain").stdout.strip():
        raise SystemExit("代码还有没提交的改动，先提交再升级。")
    from importlib.metadata import version
    label = f"{version('ccim')}-{_git('rev-parse', '--short', 'HEAD').stdout.strip()}"
    dest = os.path.join(_stable(), label)
    if not stable_python(label):
        _rm(dest)
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        for cmd in (["uv", "venv", "-q", "--python", sys.executable, dest],
                    ["uv", "pip", "install", "-q", "--compile-bytecode", "--python", os.path.join(dest, "bin", "python"),
                     f"git+file://{SRC}@{_git('rev-parse', 'HEAD').stdout.strip()}"]):
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if r.returncode != 0:
                _rm(dest)
                raise SystemExit(f"装稳定版失败：{(r.stderr or r.stdout).strip()[-500:]}")
    r = subprocess.run([os.path.join(dest, "bin", "python"), "-P", "-c", "import ccim.cli, ccim.channels.feishu"],
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
        name = os.path.basename(path)
        if which(path, e)[1] == "开发版":
            continue
        if e.get("stable_version"):
            registry.update(path, stable_version=None)
        r = daemon.running(path)
        if not r:
            results.append(f"「{name}」没在运行，下次启动就用新版本")
            continue
        if r[1] == "fg":
            results.append(f"「{name}」在终端前台运行，要到那个终端里手动重启")
            continue
        _wait_idle(path, wait)
        ok, _ = safe_restart(path, notify=False)
        if ok:
            results.append(f"「{name}」已升级到 {label}")
            continue
        if old:
            registry.update(path, stable_version=old)
            safe_restart(path, notify=False)
        results.append(f"「{name}」新版本起不来，已退回 {old or '原来的版本'}，日志：{daemon.log_path(path)}")
    _prune()
    return label, results


def _wait_idle(path, limit):
    """等这个项目手头的对话做完（最多 limit 秒）。"""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        chats = registry.read_state(path).get("chats") or {}
        if not any(c.get("busy") for c in chats.values()):
            return
        time.sleep(3)


def _prune():
    """只留当前、上一个、和被项目钉住的稳定版。"""
    c = _channel()
    keep = {c.get("current"), c.get("previous")} | {e.get("stable_version") for e in registry.all_projects().values()}
    for d in os.listdir(_stable()) if os.path.isdir(_stable()) else []:
        if d not in keep and os.path.isdir(os.path.join(_stable(), d)):
            _rm(os.path.join(_stable(), d))
