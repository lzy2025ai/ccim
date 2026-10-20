"""版本和网页上升级（#37）。

每台电脑报自己的版本：开发版（ccim 代码目录）显示当前提交；稳定版（~/.ccim/stable/<版本>）看目录名；
用 `uv tool install git+…` 装的发布版读安装记录里的提交，和发布地址（GitHub）上的最新提交比，落后了就能升级。

升级在独立进程里做（`python -m ccim.upgrade run`，脱离网页平台，重启平台也不影响它）：`uv tool upgrade ccim`，
装好后逐个安全重启在跑的项目（网页平台放最后）；平台没起来就装回原来那次提交再重启。结果写 ~/.ccim/upgrade.json。
uv 会把装着 ccim 的环境整个换掉，所以这个进程开头就把要用的模块都导入好，之后不再从磁盘导入 ccim 的代码。
"""
import json, os, re, shutil, subprocess, sys, time

from . import daemon, registry, runtime   # noqa: F401  先加载好，升级时磁盘上的代码会被换掉

LATEST_TTL = 3600
_latest = {}                                  # 发布地址 → (取到的时间, {commit, date})


def _job_file():
    return os.path.join(registry.HOME, "upgrade.json")


def job():
    try:
        with open(_job_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_job(**kw):
    j = dict(job(), **kw)
    tmp = _job_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)
    os.replace(tmp, _job_file())
    return j


def info():
    """这台电脑跑的 ccim：{version, commit, kind: dev / stable / release / local, url}。"""
    import importlib.metadata as md
    try:
        version = md.version("ccim")
    except md.PackageNotFoundError:
        version = ""
    out = {"version": version, "commit": "", "kind": "local", "url": ""}
    if runtime._is_dev_install():
        try:                                  # 开发版装的时候的版本号可能是旧的，以代码目录里的为准
            with open(os.path.join(runtime.SRC, "pyproject.toml"), encoding="utf-8") as f:
                out["version"] = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
        except (OSError, AttributeError):
            pass
        r = subprocess.run(["git", "-C", runtime.SRC, "log", "-1", "--format=%h %cI"], capture_output=True, text=True)
        commit, _, date = r.stdout.strip().partition(" ")
        return dict(out, kind="dev", commit=commit, date=date)
    real = runtime._env(sys.executable)
    stable = os.path.realpath(runtime._stable()) + os.sep
    if real.startswith(stable):
        name = os.path.relpath(real, stable.rstrip(os.sep)).split(os.sep)[0]
        return dict(out, kind="stable", commit=name.rsplit("-", 1)[-1] if "-" in name else "")
    try:
        d = json.loads(md.distribution("ccim").read_text("direct_url.json") or "{}")
    except Exception:
        d = {}
    vcs = d.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and vcs.get("commit_id"):
        return dict(out, kind="release", commit=vcs["commit_id"][:7], url=d.get("url") or "")
    deployed = os.path.expanduser("~/ccim-hub/deployed.txt")       # 服务器上的总控：部署时记下的提交
    if os.path.exists(deployed):
        with open(deployed, encoding="utf-8") as f:
            return dict(out, commit=f.read().split()[0][:7])
    return out


def latest(url, block=False):
    """发布地址上最新的提交（GitHub），一小时问一次。取不到返回 None。"""
    m = re.match(r"(?:git\+)?https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url or "")
    if not m:
        return None
    hit = _latest.get(url)
    if hit and time.time() - hit[0] < LATEST_TTL:
        return hit[1]
    if not block:                             # 网页要状态时不等网络：后台去问，这次先用上次的
        _latest[url] = (time.time() - LATEST_TTL + 60, hit[1] if hit else None)   # 一分钟内不重复问
        import threading
        threading.Thread(target=latest, args=(url, True), daemon=True).start()
        return hit[1] if hit else None
    try:
        import httpx
        r = httpx.get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/commits/HEAD", timeout=8,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        d = r.json()
        v = {"commit": d["sha"][:7], "date": ((d.get("commit") or {}).get("committer") or {}).get("date")}
    except Exception:
        v = hit[1] if hit else None
    _latest[url] = (time.time(), v)
    return v


def status():
    """给网页：版本、有没有新版本、正在升级或上次升级的结果。"""
    i = info()
    out = dict(i, job=job() or None)
    if i["kind"] == "release":
        lt = latest(i["url"])
        if lt:
            out["latest"] = lt
            out["update"] = lt["commit"] != i["commit"]
        out["can_upgrade"] = bool(_uv())
    return out


def _uv():
    home = os.path.expanduser("~")
    for p in [shutil.which("uv"), os.path.join(home, ".local/bin/uv"), os.path.join(home, ".cargo/bin/uv"),
              "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]:
        if p and os.path.exists(p):
            return p
    return None


def busy():
    """这台电脑上正在干活的对话（重启会打断）：[说明]。"""
    out = []
    for path, e in registry.all_projects().items():
        st = registry.read_state(path)
        for c in (st.get("chats") or {}).values():
            if c.get("busy"):
                out.append(c.get("title") or c.get("asked") or os.path.basename(path))
    return out


def start():
    """网页上点了升级：另起独立进程去做，马上返回。"""
    i = info()
    if i["kind"] != "release":
        raise ValueError("这台电脑跑的不是发布版，不能在这里升级。")
    if not _uv():
        raise ValueError("这台电脑上找不到 uv，到那台电脑的终端里运行 uv tool upgrade ccim。")
    j = job()
    if j.get("state") == "running" and time.time() - j.get("at", 0) < 900:
        raise ValueError("正在升级，等一下。")
    _save_job(state="running", step="下载新版本", at=time.time(), **{"from": i["commit"]}, to=None, error=None)
    log = open(os.path.join(registry.HOME, "upgrade.log"), "ab")
    subprocess.Popen([sys.executable, "-P", "-m", "ccim.upgrade", "run"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, cwd="/")
    return job()


def _new_commit():
    """装好以后的提交：用新装的 python 现问（这个进程里加载的还是旧代码）。"""
    r = subprocess.run([sys.executable, "-P", "-c", "import json,importlib.metadata as m;"
                        "print(json.loads(m.distribution('ccim').read_text('direct_url.json'))['vcs_info']['commit_id'][:7])"],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def _restart_all():
    """逐个安全重启在跑的项目，网页平台放最后。返回平台是否起来了。"""
    paths = [p for p in registry.all_projects() if daemon.running(p)]
    paths.sort(key=lambda p: (registry.get(p) or {}).get("kind") == "platform")
    ok = True
    for p in paths:
        good, msg = runtime.safe_restart(p, notify=False)
        print(msg, flush=True)
        if (registry.get(p) or {}).get("kind") == "platform":
            ok = good
    return ok


def run():
    i = info()
    old, url, uv = i["commit"], i["url"], _uv()
    try:
        r = subprocess.run([uv, "tool", "upgrade", "ccim"], capture_output=True, text=True, timeout=900)
        print(r.stdout, r.stderr, flush=True)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "升级失败")
        new = _new_commit()
        if not new or new == old:
            _save_job(state="done", step=None, to=old, note="已经是最新版", at=time.time())
            return
        _save_job(step="重启", to=new)
        if _restart_all():
            _save_job(state="done", step=None, at=time.time())
            return
        _save_job(step="新版本没起来，装回原来的版本")             # 装回原来那次提交再重启
        src = url if url.startswith("git+") else "git+" + url
        subprocess.run([uv, "tool", "install", "--force", f"{src}@{old}"], capture_output=True, text=True, timeout=900)
        _restart_all()
        _save_job(state="rolled_back", step=None, at=time.time(), error="新版本没起来，已装回原来的版本")
    except Exception as e:
        _save_job(state="failed", step=None, at=time.time(), error=str(e)[:300])


if __name__ == "__main__" and sys.argv[1:] == ["run"]:
    run()
