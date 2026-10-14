"""智能体平台：网页里和 Agent 对话、管理 Agent。

一个常驻进程，登记表里一条 kind=platform（路径 ~/.ccim/platform），daemon、安全重启都把它当项目路径用。
进程里：asyncio 主循环跑各个对话（复用 Bridge / Chat，和飞书、微信同一套排队、审批、打断、补充），
HTTP 服务跑在单独线程（ThreadingHTTPServer），接口要动对话时用 run_coroutine_threadsafe 交回主循环。

网页也是一个渠道（WebChannel）：cards=False、live=True。Chat 干活时每 0.3 秒把步骤和正在写的字推过来（progress），
做完先推最终步骤、再发回答（send_text），渠道把两者合成一条「回答」记下来。
每个网页对话一份记录：状态目录 web/<对话>.jsonl（用户消息、回答、提示、卡片、文件），打开时整份读出来；
干活中的进度在内存里，也通过 /api/events（SSE）实时推给浏览器。

对话：state.json 的 chats 里一条一个（agent 或 temp 设置、标题、session_id 等，Chat 自己也往里记）。
Agent 的对话用 Agent 的 Bridge（工作目录、角色、默认模型）；临时对话每个一个 Bridge（自己的底座、目录）。

安全：只监听 127.0.0.1；Host 必须是本机地址（挡 DNS rebinding）；接口要带 X-CCIM 头（别的网站跨域发不过来）。
"""
import asyncio, glob, json, logging, os, queue, re, shutil, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import agents, registry
from .channels.base import CardAction, Channel, Incoming

log = logging.getLogger("ccim.platform")

PORT = 8977
OWNER = "web"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
UPLOAD_LIMIT = 100 * 1024 * 1024


def home():
    return os.path.join(registry.HOME, "platform")


def ensure_registered(port=PORT):
    """平台进程在登记表里的那一条：daemon、ccim list、安全重启都靠它。"""
    h = home()
    os.makedirs(h, exist_ok=True)
    if not registry.get(h):
        registry.put(h, {"kind": "platform", "channel": "web", "bot_name": "网页", "port": port, "paired_at": registry.now()})
    return h, registry.get(h)


def url():
    h, e = ensure_registered()
    return f"http://127.0.0.1:{registry.read_state(h).get('port') or e.get('port') or PORT}/"


# ---------- 推给浏览器 ----------

class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, ev):
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    self.subs.remove(q)


# ---------- 网页对话的记录 ----------

class Logs:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.lock = threading.Lock()

    def _f(self, chat):
        return os.path.join(self.folder, re.sub(r"[^\w-]", "_", chat) + ".jsonl")

    def append(self, chat, rec):
        rec = dict(rec, at=rec.get("at") or time.time())
        with self.lock, open(self._f(chat), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def read(self, chat):
        """卡片会被更新：同一个 id 保留第一次出现的位置、最后一次的内容。"""
        out, pos = [], {}
        try:
            with open(self._f(chat), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("type") == "card" and r.get("id") in pos:
                        out[pos[r["id"]]] = dict(r, at=out[pos[r["id"]]]["at"])
                        continue
                    if r.get("type") == "card":
                        pos[r["id"]] = len(out)
                    out.append(r)
        except OSError:
            pass
        return out

    def remove(self, chat):
        try:
            os.remove(self._f(chat))
        except OSError:
            pass


# ---------- 网页渠道 ----------

class WebChannel(Channel):
    name = "web"
    label = "网页"
    cards = False
    live = True
    style = "可以用 Markdown（标题、加粗、列表、表格、代码块、链接）。"

    def __init__(self, project_path, entry, secret, handlers):
        super().__init__(project_path, entry, secret, handlers)
        self.events = Events()
        self.logs = Logs(os.path.join(registry.pdir(project_path), "web"))
        self.progress_of = {}                 # 对话 → 正在干活的进度
        self.done_steps = {}                  # 对话 → 刚做完那一轮的步骤，接下来的回答带上它
        self.card_chat = {}                   # 卡片 id → 对话
        self.server = None

    async def start(self):
        self.platform = self.handlers["platform"]
        self.server = self.platform.serve()

    async def stop(self):
        if self.server:
            self.server.shutdown()

    def _emit(self, chat, rec):
        self.events.publish(dict(rec, chat=chat))

    async def progress(self, chat_id, snap):
        if snap["state"] == "working":
            self.progress_of[chat_id] = snap
        else:
            self.progress_of.pop(chat_id, None)
            self.done_steps[chat_id] = snap
        self._emit(chat_id, {"type": "progress", **snap})

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        done = self.done_steps.pop(chat_id, None)
        if done:
            rec = {"type": "answer", "text": markdown or "", "steps": done["steps"], "state": done["state"],
                   "elapsed": done["elapsed"]}
        else:
            rec = {"type": "note", "text": markdown or ""}
        self._emit(chat_id, self.logs.append(chat_id, rec))

    async def send_card(self, chat_id, card, reply_to=None):
        mid = "card-" + uuid.uuid4().hex[:10]
        self.card_chat[mid] = chat_id
        self._emit(chat_id, self.logs.append(chat_id, {"type": "card", "id": mid, **card}))
        return mid

    async def update_card(self, message_id, card):
        chat = self.card_chat.get(message_id)
        if chat:
            self._emit(chat, self.logs.append(chat, {"type": "card", "id": message_id, **card}))

    async def delete_message(self, message_id):
        pass

    async def send_file(self, chat_id, path):
        self._emit(chat_id, self.logs.append(chat_id, {"type": "file", "path": os.path.realpath(path),
                                                       "name": os.path.basename(path), "size": os.path.getsize(path)}))

    async def download(self, message_id, kind, key, name, folder):
        """网页上传的文件先放在平台的 uploads 里，这里挪到项目的收件箱。"""
        dest = os.path.join(folder, f"{time.strftime('%m%d-%H%M%S')}-{re.sub(r'[/\\\\]', '_', name or 'file')}")
        shutil.copy(key, dest)
        return dest

    async def send_to_user(self, user_id, markdown):
        return None


# ---------- 对话调度 ----------

class Store:
    """网页对话的记录都在平台的 state.json 的 chats 里。"""

    def __init__(self, home):
        self.home = home

    def read(self):
        return registry.read_state(self.home).get("chats") or {}

    def write(self, chats):
        registry.write_state(self.home, chats=chats)


class Problem(Exception):
    """给用户看的错误。"""


class Platform:
    def __init__(self, home, entry):
        self.home, self.entry = home, entry
        self.channel = None
        self.loop = None
        self.store = Store(home)
        self.bridges = {}                     # Agent 编号或 temp:<对话> → Bridge
        self.background = set()

    def handlers(self):
        return {"platform": self}

    def attach(self, channel):
        self.channel = channel
        self.loop = asyncio.get_event_loop()

    # ----- 对话 -----

    def chats(self):
        return self.store.read()

    def meta(self, cid):
        c = self.chats().get(cid)
        if not c:
            raise Problem("找不到这个对话。")
        return c

    def _bridge(self, cid):
        from .bridge import Bridge
        m = self.meta(cid)
        if m.get("agent"):
            a = agents.get(m["agent"])
            if not a:
                raise Problem("这个对话的 Agent 已经删掉了。")
            key, path = a["id"], agents.cwd(a)
            entry = {"agent": None if a.get("base") == "claude" else a.get("base"), "model": a.get("model"),
                     "effort": a.get("effort"), "role": a.get("role")}
            name = a["name"]
        else:
            t = m.get("temp") or {}
            key = "temp:" + cid
            path = t.get("dir") or os.path.join(registry.HOME, "scratch", cid)
            os.makedirs(path, exist_ok=True)
            entry = {"agent": None if t.get("base", "claude") == "claude" else t["base"], "model": t.get("model"),
                     "effort": t.get("effort")}
            name = "临时对话"
        entry.update(owner_open_id=OWNER, channel="web")
        b = self.bridges.get(key)
        if not b or b.path != path:
            b = self.bridges[key] = Bridge(path, entry, store=self.store, home=self.home)
            b.name = name                     # 审批卡片标题用它，要在 attach 之前
            b.attach(self.channel)
        else:
            b.entry.update(entry)             # Agent 改了设置：下一个新对话用新的
            b.name = b.approvals.project = name
        return b

    def new_chat(self, agent=None, temp=None, fork_from=None, title=None, source=None):
        cid = "w-" + uuid.uuid4().hex[:10]
        rec = {"type": "p2p", "created": registry.now(), "last_active": registry.now()}
        if agent:
            if not agents.get(agent):
                raise Problem("找不到这个 Agent。")
            rec["agent"] = agent
        else:
            t = temp or {}
            d = (t.get("dir") or "").strip()
            if d:
                d = os.path.realpath(os.path.expanduser(d))
                if not os.path.isdir(d):
                    raise Problem("找不到这个文件夹。")
            rec["temp"] = {"base": t.get("base") if t.get("base") in ("claude", "codex") else "claude", "dir": d or None,
                           "model": t.get("model") or None, "effort": t.get("effort") or None}
        if fork_from:                         # 接着别处的对话聊：origin 留着，网页里把之前的内容也显示出来
            rec.update(fork_from=fork_from, origin=fork_from, source=source)
        if title:
            rec["title"] = title
        chats = self.chats()
        chats[cid] = rec
        self.store.write(chats)
        return cid

    async def send(self, cid, text, files=()):
        b = self._bridge(cid)
        m = self.meta(cid)
        if m.get("fork_from") and cid not in b.chats:
            b.chat(cid, "p2p", m["fork_from"])
        mid = "u-" + uuid.uuid4().hex[:10]
        shown = [{"name": f["name"], "size": f.get("size")} for f in files]
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "id": mid, "text": text, "files": shown}))
        inc = Incoming(chat_id=cid, chat_type="p2p", sender_id=OWNER, message_id=mid, text=text,
                       files=[("file", f["key"], f["name"]) for f in files], kind="text" if text else "file")
        await b.on_message(inc)

    async def stop_chat(self, cid):
        b = self._bridge(cid)
        c = b.chats.get(cid)
        return bool(c and await c.stop())

    async def action(self, cid, mid, value):
        b = self._bridge(cid)
        card, toast = await b.on_action(CardAction(chat_id=cid, message_id=mid, operator_id=OWNER, value=value or {}))
        if card:
            await self.channel.update_card(mid, card)
        return {"toast": toast}

    async def set_chat(self, cid, key, value):
        b = self._bridge(cid)
        c = b.chat(cid, "p2p")
        if value:
            c.set(**{key: value})
        else:
            c.follow(key)
        return self.chat_view(cid)

    def delete_chat(self, cid):
        chats = self.chats()
        if cid in chats and chats[cid].get("busy"):
            raise Problem("它还在干活，先停下再删。")
        chats.pop(cid, None)
        self.store.write(chats)
        self.channel.logs.remove(cid)

    # ----- 给网页的数据 -----

    def _effective(self, m, c=None):
        """这个对话实际用的底座、模型、思考深度（和是不是跟随默认）。"""
        a = agents.get(m["agent"]) if m.get("agent") else None
        t = m.get("temp") or {}
        base = (a or {}).get("base") or t.get("base") or "claude"
        g = registry.global_config()
        px = "" if base == "claude" else "codex_"
        model = (c or {}).get("model") or (a or t).get("model") or g.get(px + "model")
        effort = (c or {}).get("effort") or (a or t).get("effort") or g.get(px + "effort")
        dm, de = _defaults(base, agents.cwd(a) if a else (t.get("dir") or os.path.expanduser("~")))
        return {"base": base, "model": model or dm, "effort": effort or de,
                "model_custom": bool((c or {}).get("model")), "effort_custom": bool((c or {}).get("effort"))}

    def chat_view(self, cid):
        m = self.meta(cid)
        a = agents.get(m["agent"]) if m.get("agent") else None
        t = m.get("temp") or {}
        return {"id": cid, "title": m.get("title") or "新对话", "agent": m.get("agent"),
                "agent_name": a["name"] if a else None, "temp": bool(not m.get("agent")),
                "dir": (a.get("dir") if a else t.get("dir")), "busy": bool(m.get("busy")), "source": "网页",
                "origin_source": m.get("source"),
                "last_active": m.get("last_active"), "created": m.get("created"), **self._effective(m, m)}

    def chat_detail(self, cid):
        if cid.startswith("x:"):
            return self.external_detail(cid)
        v = self.chat_view(cid)
        v["log"] = self.channel.logs.read(cid)
        v["progress"] = self.channel.progress_of.get(cid)
        m = self.meta(cid)
        if m.get("origin"):
            from . import history
            a = agents.get(m["agent"]) if m.get("agent") else None
            v["earlier"] = history.read(v["base"], m["origin"], agents.cwd(a) if a else None)
        return v

    def chat_list(self):
        rows = [self.chat_view(cid) for cid in self.chats()] + self.external_chats()
        rows.sort(key=lambda r: r.get("last_active") or "", reverse=True)
        return rows

    # ----- 飞书、微信、终端里的对话 -----

    def external_chats(self, aid=None, terminal=False):
        """各 Agent 在飞书、微信里的对话（terminal=True 时加上终端、桌面端里开的）。网页里接着聊过的不重复列。"""
        mine = set()                          # 网页对话自己的会话（接着别处聊出来的分支有新编号，原来那个照常列出）
        for c in self.chats().values():
            mine |= {c.get("session_id"), c.get("codex_session_id")}
        rows = []
        for a_id, a in agents.all_agents().items():
            if aid and a_id != aid:
                continue
            a = dict(a, id=a_id)
            base, cwd = a.get("base") or "claude", agents.cwd(a)
            key = "codex_session_id" if base == "codex" else "session_id"
            for where, k, c in registry.all_chats(cwd):
                sid = c.get(key)
                if not sid or sid in mine:
                    continue
                mine.add(sid)
                kind = "群「%s」" % c["name"] if c.get("name") else "群聊" if c.get("type") == "group" else "私聊"
                from . import history
                t = c.get("title") or history.title(base, sid, cwd) or f"{where}{kind}"
                rows.append({"id": "x:" + sid, "sid": sid, "agent": a_id, "agent_name": a["name"], "source": where,
                             "title": t, "last_active": c.get("last_active"),
                             "busy": bool(c.get("busy")), "base": base, "external": True, "dir": a.get("dir")})
            if terminal and base == "claude":
                try:
                    from claude_agent_sdk import list_sessions
                    sessions = list_sessions(directory=cwd, limit=15)
                except Exception:
                    sessions = []
                for x in sessions:
                    if x.session_id in mine:
                        continue
                    mine.add(x.session_id)
                    title = " ".join((x.custom_title or x.summary or x.first_prompt or "终端里的对话").split())[:30]
                    rows.append({"id": "x:" + x.session_id, "sid": x.session_id, "agent": a_id, "agent_name": a["name"],
                                 "source": "终端", "title": title, "base": base, "external": True, "busy": False,
                                 "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x.last_modified / 1000)),
                                 "dir": a.get("dir")})
        return rows

    def _external(self, cid):
        sid = cid[2:]
        for r in self.external_chats(terminal=True):
            if r["sid"] == sid:
                return r
        raise Problem("找不到这个对话。")

    def external_detail(self, cid):
        from . import history
        r = self._external(cid)
        a = agents.get(r["agent"])
        return dict(r, log=history.read(r["base"], r["sid"], agents.cwd(a)), progress=None, readonly=True,
                    model=None, effort=None, temp=False)

    def continue_chat(self, cid, text, files):
        """在网页里接着别处的对话聊：另开一个网页对话，从那个对话分支出来，原来那边不受影响。"""
        r = self._external(cid)
        new = self.new_chat(agent=r["agent"], fork_from=r["sid"], title=r["title"], source=r["source"])
        send(self, new, text, files)
        return {"id": new}

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def bg(self, coro):
        """不等结果的（发消息：回答通过 SSE 推过去）。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: f.exception() and log.error("处理出错", exc_info=f.exception()))

    def ask(self, req):
        pass

    async def shutdown(self):
        for b in list(self.bridges.values()):
            await b.shutdown()

    # ----- HTTP -----

    def serve(self):
        port = self.entry.get("port") or PORT
        server = None
        for p in range(port, port + 20):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", p), _handler(self))
                break
            except OSError:
                continue
        if not server:
            raise RuntimeError(f"端口 {port}～{port + 19} 都被占用了")
        server.daemon_threads = True
        registry.write_state(self.home, port=server.server_address[1])
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        log.info("网页：http://127.0.0.1:%d/", server.server_address[1])
        return server


def _defaults(base, path):
    """不传参数时 Claude Code / Codex 自己会用的模型和思考深度（只用于显示）。"""
    if base == "codex":
        from .codex import defaults
        return defaults(path)
    m, e, _ = registry.claude_defaults(path)
    return m, e


# ---------- HTTP 接口 ----------

def _handler(pf):
    from . import web as console

    class Handler(BaseHTTPRequestHandler):
        server_version = "ccim"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if not self._host_ok() or self.headers.get("X-CCIM") != "1":
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def _api(self, fn):
            if not self._guard():
                return
            try:
                self._send(200, fn() or {})
            except (Problem, console.Problem, ValueError) as e:
                self._send(400, {"error": str(e)})
            except SystemExit as e:
                self._send(400, {"error": str(e.code)})
            except Exception as e:
                log.exception("接口出错")
                self._send(500, {"error": f"出错了：{e}"})

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/file":
                return self._file(q.get("chat", ""), q.get("path", ""))
            routes = {
                "/api/state": lambda: state(pf),
                "/api/chat": lambda: pf.chat_detail(q.get("id", "")),
                "/api/agent": lambda: agent_view(pf, q.get("id", ""), detail=True),
                "/api/logs": lambda: {"text": console.logs(q.get("proc", ""))},
                "/api/recent": lambda: {"folders": console.recent_folders()},
                "/api/job": lambda: (console.JOBS[q["id"]].view() if q.get("id") in console.JOBS else console._missing()),
            }
            if u.path in routes:
                return self._api(routes[u.path])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/upload":
                return self._api(self._upload)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = {}
            b = body.get
            routes = {
                "/api/chat/new": lambda: {"id": pf.new_chat(agent=b("agent"), temp=b("temp"))},
                "/api/chat/send": lambda: send(pf, b("id"), b("text") or "", b("files") or []),
                "/api/chat/continue": lambda: pf.continue_chat(b("id"), b("text") or "", b("files") or []),
                "/api/chat/stop": lambda: {"stopped": pf.run(pf.stop_chat(b("id")))},
                "/api/chat/action": lambda: pf.run(pf.action(b("id"), b("mid"), b("value"))),
                "/api/chat/set": lambda: pf.run(pf.set_chat(b("id"), b("key"), b("value"))),
                "/api/chat/delete": lambda: pf.delete_chat(b("id")),
                "/api/agent/save": lambda: save_agent(b("id"), b("fields") or {}),
                "/api/agent/delete": lambda: delete_agent(pf, b("id")),
                "/api/agent/connect": lambda: connect_agent(b("id"), b("channel")),
                "/api/channel/start": lambda: console.start(b("proc")),
                "/api/channel/stop": lambda: console.stop(b("proc")),
                "/api/channel/restart": lambda: console.restart(b("proc")),
                "/api/channel/disconnect": lambda: console.disconnect(b("path"), b("kind"), b("proc")),
                "/api/global": lambda: save_global(body),
                "/api/pick": lambda: {"path": console.pick_folder()},
                "/api/reveal": lambda: console.reveal(b("path")),
            }
            if u.path in routes:
                return self._api(routes[u.path])
            self._send(404, {"error": "not found"})

        def _upload(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > UPLOAD_LIMIT:
                raise Problem("文件太大了（最多 100 MB）。")
            from urllib.parse import unquote
            name = re.sub(r"[/\\\x00]", "_", unquote(self.headers.get("X-Name") or "file"))   # 网页那边 encodeURIComponent 过
            d = os.path.join(registry.pdir(pf.home), "uploads")
            os.makedirs(d, exist_ok=True)
            key = os.path.join(d, uuid.uuid4().hex[:10] + "-" + name)
            left = n
            with open(key, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return {"key": key, "name": name, "size": n}

        def _file(self, chat, path):
            """对话里 Agent 发出来的文件：只给这个对话记录里出现过的。"""
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            ok = any(r.get("type") == "file" and r.get("path") == path for r in pf.channel.logs.read(chat))
            if not ok or not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            import mimetypes
            with open(path, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            self._send(200, data, ctype, {"Content-Disposition": f"inline; filename*=UTF-8''{quote(os.path.basename(path))}"})

        def _events(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            q = pf.channel.events.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(b": hi\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        data = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        data = ": ping\n\n"           # 保活，也让断开的连接尽快发现
                    self.wfile.write(data.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                pf.channel.events.unsubscribe(q)
                self.close_connection = True

    return Handler


# ---------- 接口用到的操作 ----------

def send(pf, cid, text, files):
    text = (text or "").strip()
    if not text and not files:
        raise Problem("消息是空的。")
    pf.meta(cid)
    for f in files:
        if not os.path.isfile(f.get("key", "")) or not os.path.realpath(f["key"]).startswith(
                os.path.realpath(os.path.join(registry.pdir(pf.home), "uploads")) + os.sep):
            raise Problem("文件没传上来，再试一次。")
    pf.bg(pf.send(cid, text, files))
    return {"ok": True}


def _busy_of(a):
    """这个 Agent 在各处（网页、飞书、微信）有没有在干活，在干什么。"""
    from . import web as console
    rows = []
    for kind, proc, path in agents.linked_paths(a["id"]):
        rows += [c for c in console._all_chats(proc) if c.get("busy")]
    return rows


def agent_view(pf, aid, detail=False):
    from . import web as console
    a = agents.get(aid)
    if not a:
        raise Problem("找不到这个 Agent。")
    web_busy = [c for c in pf.chats().values() if c.get("agent") == aid and c.get("busy")]
    busy = web_busy + _busy_of(a)
    chans = []
    seen = set()
    for kind, proc, path in agents.linked_paths(aid):
        for ch in console._channels(path):
            if ch["proc"] not in seen and ((kind == "feishu" and ch["kind"] != "weixin") or (kind == "weixin" and ch["kind"] == "weixin")):
                seen.add(ch["proc"])
                chans.append(dict(ch, path=path))
    view = {**a, "cwd": agents.cwd(a), "busy": bool(busy), "doing": ((busy[0].get("steps") or [None])[-1] if busy else None),
            "channels": chans, "defaults": dict(zip(("model", "effort"), _defaults(a.get("base") or "claude", agents.cwd(a))))}
    view["last_active"] = max([c.get("last_active") or "" for c in pf.chats().values() if c.get("agent") == aid] +
                              [c.get("last_active") or "" for _, _, c in registry.all_chats(agents.cwd(a))] + [""]) or None
    if detail:
        view["chats"] = [pf.chat_view(cid) for cid, c in pf.chats().items() if c.get("agent") == aid] + \
            pf.external_chats(aid, terminal=True)
        view["chats"].sort(key=lambda r: r.get("last_active") or "", reverse=True)
    return view


def state(pf):
    from . import web as console
    g = registry.global_config()
    return {"agents": [agent_view(pf, aid) for aid in agents.all_agents()], "chats": pf.chat_list(),
            "global": g, "defaults": {"claude": _defaults("claude", os.path.expanduser("~")),
                                      "codex": _defaults("codex", os.path.expanduser("~"))},
            "codex": console._codex(), "home": os.path.expanduser("~")}


def save_agent(aid, fields):
    if fields.get("base") == "codex":
        from . import web as console
        if not console._codex():
            raise Problem("这台电脑上还没装 Codex。装好 ChatGPT 桌面版后再选。")
    if fields.get("dir"):
        d = os.path.realpath(os.path.expanduser(fields["dir"].strip()))
        if not os.path.isdir(d):
            raise Problem("找不到这个工作目录。")
        if d in ("/", os.path.realpath(os.path.expanduser("~"))) and not fields.get("confirmed"):
            raise Problem("工作目录不能是整个硬盘或用户主目录，选一个具体的文件夹。")
    if aid:
        a = agents.update(aid, **fields)
    else:
        a = agents.create(**fields)
    return {"id": a["id"]}


def delete_agent(pf, aid):
    from . import web as console
    a = agents.get(aid)
    if not a:
        return {}
    if any(c.get("agent") == aid and c.get("busy") for c in pf.chats().values()):
        raise Problem("它还在干活，先停下再删。")
    for kind, proc, path in agents.linked_paths(aid):
        console.disconnect(path, kind, proc)
    agents.remove(aid)
    return {}


def connect_agent(aid, channel):
    """给 Agent 接飞书或微信：扫码（复用控制台的接入流程），接好后把登记关联到这个 Agent。"""
    from . import web as console
    a = agents.get(aid)
    if not a:
        raise Problem("找不到这个 Agent。")
    path = agents.cwd(a)
    if channel == "feishu" and registry.get(path) and registry.get(path).get("agent_id") not in (None, aid):
        other = agents.get(registry.get(path)["agent_id"])
        raise Problem(f"这个工作目录的飞书机器人已经给了「{(other or {}).get('name', '另一个 Agent')}」。")
    job = console.connect(path, channel, a.get("base") or "claude", confirmed=True)
    if job.get("id") in console.JOBS:
        console.JOBS[job["id"]].agent_id = aid
    return job


def save_global(body):
    keys = ("model", "effort", "codex_model", "codex_effort")
    return registry.set_global(**{k: (body.get(k) or "").strip() or None for k in keys if k in body})


# ---------- ccim web ----------

def open_in_browser():
    """确保平台在后台常驻，然后在浏览器里打开。"""
    import webbrowser
    from . import daemon
    h, _ = ensure_registered()
    if not daemon.running(h):
        print("正在启动…", flush=True)
        daemon.start_always(h, "web")
    u = url()
    print(f"ccim：{u}", flush=True)
    webbrowser.open(u)
