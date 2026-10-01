"""前台运行 / 后台运行 / 常驻（launchd）/ 查在线状态 / 关停。

每个项目一个进程。进程启动后把 pid、运行方式写进 ~/.ccim/projects/<key>/state.json，
另外每 30 秒写一次心跳；在不在线以「pid 活着且确实是 ccim 进程」为准。

运行方式：fg 前台；bg 后台（脱离终端，进程没了就没了）；
always 常驻：交给 macOS 的 launchd 托管，登录后自动启动，崩溃或启动失败（比如开机时还没联网）30 秒后自动重启。
"""
import os, plistlib, signal, subprocess, sys, time

from . import registry


def _is_ccim(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "ccim" in args


def running(path):
    """在跑就返回 (pid, 运行方式 fg/bg)，否则 None。"""
    s = registry.read_state(path)
    pid = s.get("pid")
    if pid and _is_ccim(pid):
        return pid, s.get("mode", "fg")
    return None


def log_path(path):
    return os.path.join(registry.pdir(path), "ccim.log")


def _argv(path, channel, mode):
    return [sys.executable, "-m", "ccim.cli", "run", path, "--channel", channel, "--mode", mode]


def start_background(path, channel):
    """脱离终端在后台跑：关掉终端、关掉 Claude 桌面版都不影响。"""
    log = open(log_path(path), "ab")
    started = registry.now()
    proc = subprocess.Popen(_argv(path, channel, "bg"), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True, cwd=path)
    return _wait_online(path, started, exited=lambda: proc.poll() is not None)


def _wait_online(path, started, exited):
    """等它连上 IM（最多 45 秒；第一次加载飞书 SDK 比较慢）。返回 pid。"""
    for _ in range(180):
        time.sleep(0.25)
        s = registry.read_state(path)
        fresh = s.get("started", "") >= started
        if fresh and s.get("online") and s.get("pid") and _is_ccim(s["pid"]):
            return s["pid"]
        err = s.get("error") if s.get("error_at", "") >= started else None
        if exited() or err:
            raise SystemExit(f"启动失败：{err or _last_line(log_path(path))}\n日志：{log_path(path)}")
    raise SystemExit(f"45 秒还没连上，日志：{log_path(path)}")


# ---------- 常驻：launchd ----------

def label(path):
    return f"com.ccim.{registry.key(path)}"


def plist_path(path):
    return os.path.expanduser(f"~/Library/LaunchAgents/{label(path)}.plist")


def is_always(path):
    return os.path.exists(plist_path(path))


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain():
    return f"gui/{os.getuid()}"


def start_always(path, channel):
    """写 LaunchAgent 并加载：登录后自动启动，异常退出 30 秒后重启；ccim stop 才真正停。"""
    home = os.path.expanduser("~")
    env_path = ":".join([os.path.join(home, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                         "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    plist = {
        "Label": label(path),
        "ProgramArguments": _argv(path, channel, "always"),
        "WorkingDirectory": path,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # 正常退出（ccim stop）不拉起，崩溃、启动失败才拉起
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "StandardOutPath": log_path(path),
        "StandardErrorPath": log_path(path),
        "EnvironmentVariables": {"PATH": env_path, "LANG": "zh_CN.UTF-8", "PYTHONUNBUFFERED": "1"},
    }
    os.makedirs(os.path.dirname(plist_path(path)), exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{label(path)}")
    with open(plist_path(path), "wb") as f:
        plistlib.dump(plist, f)
    started = registry.now()
    r = _launchctl("bootstrap", _domain(), plist_path(path))
    if r.returncode != 0:
        os.remove(plist_path(path))
        raise SystemExit(f"交给系统托管失败：{(r.stderr or r.stdout).strip()}")
    try:
        return _wait_online(path, started, exited=lambda: False)
    except SystemExit:
        # 启动失败时 launchd 会每 30 秒重试；这里不撤销，让它在网络恢复后自己连上
        print("（系统会每 30 秒自动重试；不想要了就 ccim stop）")
        raise


def restart_always(path):
    started = registry.now()
    _launchctl("kickstart", "-k", f"{_domain()}/{label(path)}")
    return _wait_online(path, started, exited=lambda: False)


def _stop_always(path):
    _launchctl("bootout", f"{_domain()}/{label(path)}")
    try:
        os.remove(plist_path(path))
    except FileNotFoundError:
        pass


def _last_line(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        return lines[-1] if lines else "没有输出"
    except OSError:
        return "没有日志"


def stop(path):
    """关停。常驻的会同时撤掉系统托管，以后登录也不再自动启动。"""
    was_always = is_always(path)
    if was_always:
        _stop_always(path)
    r = running(path)
    if not r:
        return was_always
    os.kill(r[0], signal.SIGTERM)
    for _ in range(40):
        if not _is_ccim(r[0]):
            break
        time.sleep(0.1)
    registry.write_state(path, online=False, pid=None)
    return True
