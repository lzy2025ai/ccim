"""ccim web：本机控制台。在浏览器里看各项目的状态、接入新项目、启动停止、改设置、看日志。

只监听 127.0.0.1。别的网页也能往 127.0.0.1 发请求，所以：Host 必须是本机地址（挡 DNS rebinding），
接口请求必须带 X-CCIM 头（跨域带自定义头要先预检，我们不答预检，别的网站就发不过来）。
页面是同目录的 web.html，接口都在 /api/ 下，返回 JSON。
扫码接入在后台线程里跑（feishu.pair / cli.add_weixin 本身是阻塞的），二维码通过 show 回调交给网页，网页轮询任务状态。
"""
import glob, json, os, secrets, subprocess, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import daemon, registry, runtime, weixin_hub

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
MODELS = ("opus", "sonnet", "haiku", "fable")


# ---------- 读状态 ----------

def _proc(proc):
    """一个 ccim 进程（飞书项目或微信号）的状态。"""
    r = daemon.running(proc)
    s = registry.read_state(proc)
    always = daemon.is_always(proc)
    if r and s.get("error"):
        state = "error"
    elif r:
        state = "online" if s.get("online") else "connecting"
    elif always:
        state = "retrying"                     # 常驻的进程没了：launchd 30 秒后会拉起
    else:
        state = "stopped"
    return {"proc": proc, "state": state, "error": s.get("error") if state in ("error", "retrying") else None,
            "always": always, "foreground": bool(r and r[1] == "fg"),
            "version": (runtime.describe(s.get("python")) if r else ""), "fallback": bool(r and s.get("fallback"))}


def _chats_of(proc, path):
    s = registry.read_state(proc)
    chats = s.get("chats") or {}
    return chats.get(path) or {} if registry.get(proc).get("kind") == "weixin" else chats


def _settings(path):
    """项目自己的设置（模型、思考深度、助手）：飞书的记在登记表，只接了微信的记在微信号的 projects 里。"""
    e = registry.get(path)
    if e:
        return e
    for _, h in weixin_hub.hubs_of(path):
        return (h.get("projects") or {}).get(path) or {}
    return {}


def _effective(path, settings, model=None, effort=None):
    codex = settings.get("agent") == "codex"
    if codex:
        from .codex import defaults
        dm, de = defaults(path)
        m, e = model or settings.get("model"), effort or settings.get("effort")
        return {"model": m or dm or "", "effort": e or de or "", "model_set": bool(m), "effort_set": bool(e)}
    g = registry.global_config()
    m, e = model or settings.get("model") or g.get("model"), effort or settings.get("effort") or g.get("effort")
    em, ee, _, _ = registry.effective(path, m, e)
    return {"model": "" if em == "默认" else em, "effort": "" if ee == "默认" else ee,
            "model_set": bool(model or settings.get("model")), "effort_set": bool(effort or settings.get("effort"))}


def _channels(path):
    out = []
    e = registry.get(path)
    if e:
        out.append({"kind": e.get("channel", "feishu"), "bot": e.get("bot_name") or e.get("app_id"),
                    "group": e.get("group") or "owner", "claim_code": e.get("claim_code"), **_proc(path)})
    for hub, h in weixin_hub.hubs_of(path):
        others = [os.path.basename(p) for p in (h.get("projects") or {}) if p != path]
        out.append({"kind": "weixin", "bot": h.get("bot_name"), "shared_with": others, **_proc(hub)})
    return out


def _all_paths():
    paths = [p for p, e in registry.all_projects().items() if e.get("kind") != "weixin"]
    for _, h in weixin_hub.hubs():
        paths += [p for p in (h.get("projects") or {}) if p not in paths]
    return paths


def project(path, detail=False):
    settings = _settings(path)
    chans = _channels(path)
    chats = registry.all_chats(path)
    live = {c["proc"] for c in chans if c["state"] in ("online", "connecting", "error")}
    busy = []
    for c in chans:
        if c["proc"] in live:
            busy += [x for x in _chats_of(c["proc"], path).values() if x.get("busy")]
    busy.sort(key=lambda x: x.get("since") or "", reverse=True)
    out = {"path": path, "name": os.path.basename(path), "home": _tilde(path), "exists": os.path.isdir(path),
           "agent": settings.get("agent") or "claude", "channels": chans, "busy": bool(busy),
           "doing": ((busy[0].get("steps") or [None])[-1] if busy else None), "since": busy[0].get("since") if busy else None,
           "last_active": max((c.get("last_active") or "" for _, _, c in chats), default="") or None,
           "chat_count": len(chats), **_effective(path, settings)}
    if detail:
        out["settings"] = {k: settings.get(k) for k in ("model", "effort", "agent")}
        codex = out["agent"] == "codex"
        rows = []
        for where, key, c in sorted(chats, key=lambda x: x[2].get("last_active") or "", reverse=True):
            eff = _effective(path, settings, c.get("model"), c.get("effort"))
            rows.append({"where": where, "type": c.get("type") or "p2p", "topic": "|" in key, "name": c.get("name"),
                         "title": c.get("title"),
                         "last_active": c.get("last_active"), "busy": bool(c.get("busy")) and bool(live),
                         "asked": c.get("asked") if c.get("busy") and live else None,
                         "steps": (c.get("steps") or []) if c.get("busy") and live else [], "since": c.get("since"),
                         "custom": bool(c.get("model") or c.get("effort")), "model": eff["model"], "effort": eff["effort"],
                         "session": (c.get("codex_session_id" if codex else "session_id") or "")[:8]})
        out["chats"] = rows
    return out


def overview():
    return {"projects": [project(p) for p in _all_paths()], "global": registry.global_config(),
            "codex": _codex(), "home": os.path.expanduser("~")}


def _codex():
    from .codex import find_codex
    return bool(find_codex())


def _tilde(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + os.sep) else p


# ---------- 操作 ----------

class Problem(Exception):
    """给用户看的错误。"""


def _known_proc(proc):
    e = registry.get(proc)
    if not e:
        raise Problem("找不到这个项目，可能已经断开了。")
    return e


def start(proc):
    e = _known_proc(proc)
    r = daemon.running(proc)
    if r:
        return
    daemon.start_always(proc, e["channel"])


def stop(proc):
    _known_proc(proc)
    daemon.stop(proc)


def restart(proc):
    e = _known_proc(proc)
    r = daemon.running(proc)
    if r and r[1] == "fg":
        raise Problem("它正在终端里运行，到那个终端按 Ctrl+C 停掉，再在这里启动。")
    always = daemon.is_always(proc) or not r
    daemon.stop(proc)
    if always:
        daemon.start_always(proc, e["channel"])
    else:
        daemon.start_background(proc, e["channel"])


def save_settings(path, changes):
    """改项目设置。飞书的登记和各微信号里这个项目的设置一起改，项目只有一份设置。
    正在运行、没在干活的进程重启生效；在干活的不打断，返回它们，网页提示稍后重启。"""
    allowed = {"model": None, "effort": EFFORTS + CODEX_EFFORTS, "agent": ("claude", "codex"), "group": ("owner", "all")}
    clean = {}
    for k, v in changes.items():
        if k not in allowed:
            continue
        v = (v or "").strip() or None
        if v and allowed[k] and v not in allowed[k]:
            raise Problem(f"{v} 不是可选的值。")
        clean[k] = None if (k == "agent" and v == "claude") or (k == "group" and v == "owner") else v
    if clean.get("agent") == "codex" and not _codex():
        raise Problem("这台电脑上还没装 Codex。装好 ChatGPT 桌面版后再切换。")
    procs = []
    if registry.get(path):
        registry.update(path, **clean)
        procs.append(path)
    for hub, h in weixin_hub.hubs_of(path):
        projects = dict(h.get("projects") or {})
        cur = dict(projects.get(path) or {})
        cur.update({k: v for k, v in clean.items() if k in weixin_hub.SETTINGS})
        projects[path] = {k: v for k, v in cur.items() if v}
        registry.update(hub, projects=projects)
        procs.append(hub)
    if not procs:
        raise Problem("找不到这个项目，可能已经断开了。")
    waiting = []
    for proc in procs:
        r = daemon.running(proc)
        if not r:
            continue
        if r[1] == "fg" or any(x.get("busy") for x in _all_chats(proc)):
            waiting.append(proc)
            continue
        restart(proc)
    return {"waiting": waiting}


def _all_chats(proc):
    chats = registry.read_state(proc).get("chats") or {}
    if registry.get(proc).get("kind") == "weixin":
        return [c for per in chats.values() for c in (per or {}).values()]
    return list(chats.values())


def save_global(changes):
    clean = {k: (changes.get(k) or "").strip() or None for k in ("model", "effort")}
    if clean["effort"] and clean["effort"] not in EFFORTS:
        raise Problem(f"{clean['effort']} 不是可选的值。")
    return registry.set_global(**clean)


def disconnect(path, kind, proc):
    """断开一个渠道：飞书是解除配对（机器人应用还留在飞书里）；微信是把项目移出这个微信号。"""
    if kind == "weixin":
        if path not in (registry.get(proc) or {}).get("projects", {}):
            raise Problem("这个项目已经不在这个微信里了。")
        weixin_hub.remove_project(proc, path)
        return
    e = registry.get(path)
    if not e:
        raise Problem("这个项目已经断开了。")
    daemon.stop(path)
    registry.del_secret(e["app_id"])
    registry.remove(path)


def logs(proc, n=300):
    try:
        with open(daemon.log_path(proc), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return ""
    return "".join(lines)


def reveal(path):
    if os.path.isdir(path):
        subprocess.run(["open", path], capture_output=True)


def pick_folder():
    """弹出系统的选择文件夹窗口。取消返回 None。"""
    script = ['tell application "System Events"', "activate",
              'set f to choose folder with prompt "选择要接到手机上的项目文件夹"', "end tell", "POSIX path of f"]
    r = subprocess.run(["osascript"] + [a for line in script for a in ("-e", line)], capture_output=True, text=True)
    p = r.stdout.strip()
    return p.rstrip("/") or "/" if r.returncode == 0 and p else None


def recent_folders(limit=6):
    """最近在 Claude Code 里打开过的文件夹（还没接入的），给「选文件夹」那一步当快捷选项。"""
    root = os.path.expanduser("~/.claude/projects")
    h = os.path.expanduser("~")
    taken = set(_all_paths()) | {h, "/"} | {os.path.join(h, d) for d in ("Desktop", "Documents", "Downloads")}
    skip = lambda p: (p in taken or not p.startswith(h + os.sep) or "/." in p[len(h):])   # 临时目录、隐藏目录（工作树等）不要
    found = []
    for d in glob.glob(os.path.join(root, "*")):
        files = glob.glob(os.path.join(d, "*.jsonl"))
        if not files:
            continue
        newest = max(files, key=os.path.getmtime)
        cwd = _cwd_of(newest)
        if cwd and not skip(cwd) and os.path.isdir(cwd) and cwd not in (p for _, p in found):
            found.append((os.path.getmtime(newest), cwd))
    found.sort(reverse=True)
    return [{"path": p, "name": os.path.basename(p), "home": _tilde(p)} for _, p in found[:limit]]


def _cwd_of(f):
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(30), fh):
                if '"cwd"' in line:
                    try:
                        return os.path.realpath(json.loads(line).get("cwd") or "") or None
                    except ValueError:
                        continue
    except OSError:
        pass
    return None


# ---------- 扫码接入 ----------

JOBS = {}


class Job:
    """一次接入：working → scan（有二维码了）→ starting → done / error。"""

    def __init__(self, path, channel, agent):
        self.id = secrets.token_hex(6)
        self.path, self.channel, self.agent = path, channel, agent
        self.state, self.qr, self.link, self.error, self.bot = "working", None, None, None, None
        JOBS[self.id] = self

    def show(self, url):
        import qrcode
        import qrcode.image.svg
        from qrcode.constants import ERROR_CORRECT_H     # 中间要盖一个项目印章
        self.qr = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=0,
                              error_correction=ERROR_CORRECT_H).to_string(encoding="unicode")
        self.link, self.state = url, "scan"

    def run(self):
        from . import cli
        try:
            if self.channel == "weixin":
                cli.add_weixin(self.path, None, self.agent, show=self.show)
                hubs = weixin_hub.hubs_of(self.path)
                self.bot = hubs[0][1].get("bot_name") if hubs else "微信"
            else:
                cli.pair(self.path, "feishu", show=self.show, interactive=False)
                cli._apply_agent(self.path, self.agent)
                self.state = "starting"
                daemon.start_always(self.path, "feishu")
                self.bot = registry.get(self.path).get("bot_name")
            self.state = "done"
        except SystemExit as e:
            self.state, self.error = "error", str(e.code if e.code is not None else "没有接入成功。")
        except Exception as e:
            self.state, self.error = "error", f"没有接入成功：{e}"

    def view(self):
        return {"id": self.id, "state": self.state, "qr": self.qr, "link": self.link, "error": self.error,
                "bot": self.bot, "channel": self.channel, "path": self.path, "name": os.path.basename(self.path)}


def connect(path, channel, agent, confirmed=False):
    path = os.path.realpath(os.path.expanduser((path or "").strip()))
    if not os.path.isdir(path):
        raise Problem("找不到这个文件夹。")
    if path == "/":
        raise Problem("不能接入整个硬盘，选一个项目文件夹。")
    if path == os.path.realpath(os.path.expanduser("~")) and not confirmed:
        return {"confirm": "home"}
    if channel not in ("feishu", "weixin"):
        raise Problem("选飞书或微信。")
    if agent == "codex" and not _codex():
        raise Problem("这台电脑上还没装 Codex。")
    if channel == "feishu" and registry.get(path):
        raise Problem("这个项目已经接到飞书了。")
    if channel == "weixin" and weixin_hub.hubs_of(path):
        raise Problem("这个项目已经接到微信了。")
    job = Job(path, channel, agent)
    threading.Thread(target=job.run, daemon=True).start()
    return job.view()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ccim"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _api(self, fn):
        if not self._host_ok() or self.headers.get("X-CCIM") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            self._send(200, fn() or {})
        except Problem as e:
            self._send(400, {"error": str(e)})
        except SystemExit as e:
            self._send(400, {"error": str(e.code)})
        except Exception as e:
            self._send(500, {"error": f"出错了：{e}"})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        routes = {
            "/api/overview": overview,
            "/api/project": lambda: project(os.path.realpath(q.get("path", "")), detail=True),
            "/api/logs": lambda: {"text": logs(q.get("proc", ""))},
            "/api/recent": lambda: {"folders": recent_folders()},
            "/api/job": lambda: (JOBS[q["id"]].view() if q.get("id") in JOBS else _missing()),
        }
        if u.path in routes:
            return self._api(routes[u.path])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        b = body.get
        routes = {
            "/api/start": lambda: start(b("proc")),
            "/api/stop": lambda: stop(b("proc")),
            "/api/restart": lambda: restart(b("proc")),
            "/api/settings": lambda: save_settings(b("path"), b("changes") or {}),
            "/api/global": lambda: save_global(body),
            "/api/disconnect": lambda: disconnect(b("path"), b("kind"), b("proc")),
            "/api/reveal": lambda: reveal(b("path")),
            "/api/pick": lambda: {"path": pick_folder()},
            "/api/connect": lambda: connect(b("path"), b("channel"), b("agent") or "claude", bool(b("confirmed"))),
        }
        if u.path in routes:
            return self._api(routes[u.path])
        self._send(404, {"error": "not found"})


def _missing():
    raise Problem("这次接入已经结束了，重新开始一次。")


def serve(port=8977, open_browser=True):
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if not server:
        raise SystemExit(f"端口 {port}～{port + 19} 都被占用了，用 --port 换一个。")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"ccim 控制台：{url}\n按 Ctrl+C 关掉。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
