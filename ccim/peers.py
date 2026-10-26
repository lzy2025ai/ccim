"""多台电脑（#13）：局域网里另一台电脑加入，在一个网页里管理几台电脑的 Agent、对话、项目、定时任务。

一台是总控（没加入别人的就是总控），其他的「加入」它。每台电脑的平台进程照旧管自己的东西（Agent、对话记录、
项目、定时任务都在本机），网页也只在本机打开（127.0.0.1）；跨电脑的事都是「把这个接口请求交给那台电脑执行」：

- 网页请求带 X-Machine 头（目标电脑），平台发现不是自己，就 forward 过去；那台电脑收到后照原样请求自己的
  127.0.0.1 接口（带 X-From-Peer），所以各接口不用改。/api/state 把各台电脑的拼在一起（每一项带 machine）。
- 连接方向：加入的电脑主动连总控（总控对局域网只开 PEER_PORT 这一个端口），以后总控放到公网也一样。
  总控 → 加入的电脑：加入的电脑一直长轮询 /peer/poll，总控把要它执行的请求、别处的推送放进去；它执行完 POST /peer/reply。
  加入的电脑 → 别处：POST /peer/call（目标是总控就总控执行，是另一台就总控转交）。
  推送（SSE）：各台电脑把自己产生的推送交给总控（/peer/push），总控转给其他电脑（带 _m 记来源，不回传）。
- 认证：配对码（一次性、10 分钟、错 10 次作废）换一把密钥；之后每个请求、每个回应都用它做 HMAC 签名
  （时间戳 + 随机数 + 内容摘要，防伪造和重放）。局域网里内容不加密。
- 离线：发消息给离线的电脑先存在本机 outbox，连上后发出。
- 管理员密码不跨电脑传：从别的电脑来的 /api/chat/secret 只能选「在那台 Mac 上弹授权窗口」。
"""
import base64, hashlib, hmac, http.client, json, logging, os, queue, secrets, socket, subprocess, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import registry

log = logging.getLogger("ccim.peers")

PEER_PORT = 8978
PROTOCOL = 1
POLL_WAIT = 20                                # 长轮询最多挂这么久
ONLINE_WINDOW = 45                            # 这么久没来轮询就算离线
SKEW = 120                                    # 签名时间戳允许的误差
CODE_TTL = 600
PASS_HEADERS = ("X-Name", "Content-Type", "X-Account", "X-Account-Name", "X-Account-Share")   # 转交时带过去的头；X-Account 只有服务器总控会加（#154）
OWNER = "owner"                               # 服务器上的总控：主人的账号编号（hubserver.OWNER）
STATE_TIMEOUT = 5
STATE_FRESH = 60                              # 别的电脑的 state 多久内直接用上次取的（有变化会推送过来，标记重取）
STATE_REFRESH = 10                            # 超过这么久，返回缓存的同时后台再取一次
CALL_TIMEOUT = 660                            # 派活最长 600 秒，留点余量
QUICK = {"/api/state", "/api/chat", "/api/agent", "/api/project", "/api/schedule", "/api/dirs", "/api/peer/task",
         "/api/peer/result"}
QUEUED = ("/api/chat/send", "/api/chat/mention")     # 离线时先存起来、上线再发的


class Offline(Exception):
    pass


class Server(ThreadingHTTPServer):
    """监听队列默认只有 5：网页一下发出很多请求、进程又正忙的时候，多出来的连接会被直接重置。"""
    request_queue_size = 128


def _file(name):
    return os.path.join(registry.HOME, name)


def _load(name, default):
    try:
        with open(_file(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, data):
    os.makedirs(registry.HOME, exist_ok=True)
    tmp = _file(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _file(name))


def identity():
    """这台电脑：编号（第一次生成）和名字（系统里的电脑名）。"""
    m = _load("machine.json", {})
    changed = not os.path.exists(_file("machine.json"))
    if not m.get("id"):                       # 编号一旦生成就不能变（别的电脑靠它认）
        m["id"] = "m" + uuid.uuid4().hex[:8]
        changed = True
    if not m.get("name"):
        try:
            m["name"] = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                                       timeout=5).stdout.strip() or socket.gethostname()
        except Exception:
            m["name"] = socket.gethostname()
        changed = True
    if changed:
        _save("machine.json", m)
    return m


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # 不会真的发出去，只是让系统选一个出口地址
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


# ---------- 签名 ----------

def _digest(body):
    return hashlib.sha256(body or b"").hexdigest()


def _sign(key, *parts):
    return hmac.new(key.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


class Nonces:
    def __init__(self):
        self.seen, self.lock = {}, threading.Lock()

    def fresh(self, nonce):
        now = time.time()
        with self.lock:
            for k in [k for k, t in self.seen.items() if now - t > SKEW * 2]:
                del self.seen[k]
            if nonce in self.seen:
                return False
            self.seen[nonce] = now
            return True


class Shared:
    """同时到的同一个请求只算一次、大家共用结果；刚算好的 TTL 秒内来的也直接用（#258）。
    几个页面收到同一条推送会差不多同时来要整体状态，各算各的就互相排队。
    有改动（POST 做完、推了会改列表的推送）就作废，之后来的重新算；改动之前开始算的那份也不给之后来的用。"""
    TTL = 3

    def __init__(self):
        self.lock = threading.Lock()
        self.gen = 0                          # 改动一次加一
        self.slots = {}                       # 键 → {gen, at 开始算的时刻, done, val, err}

    def changed(self, ev=None):
        if ev and ev.get("type") == "progress" and ev.get("state") == "working":
            return                            # 正在干活的进度每 0.3 秒一条，只是在做哪一步，不作废
        with self.lock:
            self.gen += 1

    def get(self, key, fn):
        with self.lock:
            s = self.slots.get(key)
            mine = not (s and s["gen"] == self.gen and s["err"] is None
                        and (not s["done"].is_set() or time.time() - s["at"] < self.TTL))
            if mine:
                s = self.slots[key] = {"gen": self.gen, "at": time.time(), "done": threading.Event(), "val": None, "err": None}
        if mine:
            try:
                s["val"] = fn()
            except BaseException as e:
                s["err"] = e
                raise
            finally:
                s["done"].set()
            return s["val"]
        s["done"].wait()
        if s["err"] is not None:
            raise s["err"]
        return s["val"]


# ---------- 执行本机接口 ----------

def local_request(pf, method, path, body=b"", headers=None, timeout=CALL_TIMEOUT):
    """照原样请求本机平台的接口（和网页发的一样），返回 (状态码, 内容类型, 内容)。"""
    port = registry.read_state(pf.home).get("port") or 8977
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        h = {"X-CCIM": "1", "X-From-Peer": "1", "Host": f"127.0.0.1:{port}"}
        for k in PASS_HEADERS:
            if (headers or {}).get(k):
                h[k] = headers[k]
        c.request(method, path, body=body or None, headers=h)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "application/json", r.read()
    finally:
        c.close()


def _conn(url, timeout):
    """连总控：http 或 https（服务器上的总控，证书按系统 / certifi 的根证书验证）。"""
    u = urlparse(url)
    if u.scheme == "https":
        import ssl
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(u.hostname, u.port or PEER_PORT, timeout=timeout)


class Peers:
    def __init__(self, pf, server=False, public_url=None):
        self.pf = pf
        self.server_hub = server                      # 服务器上的总控：自己不是一台干活的电脑
        self.public_url = public_url
        self.me = identity()
        self.lock = threading.RLock()
        self.nonces = Nonces()
        # 总控这边
        self.nodes = _load("peers.json", {})          # 加入的电脑：编号 → {name, key, added}
        self.codes = {}                               # 配对码 → {"until", "tries"}
        self.boxes = {}                               # 加入的电脑 → 等它取走的消息（请求、推送）
        self.seen = {}                                # 加入的电脑 → 最近一次来轮询的时间
        self.waiting = {}                             # 请求编号 → [Event, 结果]
        self.server = None
        # 加入别人的这边
        self.hub = _load("hub.json", None)            # {url, hub_id, hub_name, key}
        self.hub_ok = 0                               # 最近一次连上总控的时间
        self.machines_from_hub = []
        self.cache = _load("peer-cache.json", {})     # 各台电脑上次的 state，离线时显示
        self.cache_at = {}                    # 上次从那台电脑取 state 的时间：新的直接用，不每次都现问（#32）
        self.dirty = set()                    # 刚往那台电脑发过操作、或它有了新动静：下次要现问
        self.refreshing = set()
        self.guest_ok = {}                    # 共享出去的电脑是不是认访客的新版本（guest_ready）
        self.stopping = False

    # ----- 启动 -----

    def start(self):
        if self.hub:
            for fn in (self._poll_loop, self._push_loop, self._outbox_loop):
                threading.Thread(target=fn, name=fn.__name__, daemon=True).start()
        else:
            if self.nodes or self.server_hub:
                self.serve()
            threading.Thread(target=self._fanout_loop, name="fanout", daemon=True).start()
            threading.Thread(target=self._outbox_loop, name="outbox", daemon=True).start()

    def serve(self):
        with self.lock:
            if self.server:
                return
            try:
                # 服务器上的总控在 nginx 后面，只听本机；局域网的总控要让别的电脑连进来
                self.server = Server(("127.0.0.1" if self.server_hub else "0.0.0.0", PEER_PORT), _handler(self))
            except OSError as e:
                raise RuntimeError(f"端口 {PEER_PORT} 被占用了，别的电脑连不进来：{e}")
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, name="peers", daemon=True).start()
            log.info("等别的电脑连进来：%s 端口 %d", "127.0.0.1（前面是 nginx）" if self.server_hub else ", ".join(lan_ips()) or "本机", PEER_PORT)

    def stop(self):
        self.stopping = True
        if self.server:
            self.server.shutdown()

    # ----- 电脑列表 -----

    def is_hub(self):
        return not self.hub

    def online(self, mid):
        if mid == self.me["id"]:
            return True
        if self.is_hub():
            return time.time() - self.seen.get(mid, 0) < ONLINE_WINDOW
        if mid == self.hub["hub_id"]:
            return time.time() - self.hub_ok < ONLINE_WINDOW
        return any(m["id"] == mid and m.get("online") for m in self.machines_from_hub)

    def owner_of(self, mid):
        """服务器上的总控：这台电脑归哪个账号（#154，以前加入的都归主人）。"""
        return (self.nodes.get(mid) or {}).get("account") or OWNER

    def machines(self, account=None, own=False):
        """[{id, name, me, hub, online}]，第一个是总控。服务器上的总控给了 account 就只列这个账号的电脑。"""
        if self.is_hub():
            out = [] if self.server_hub else [{"id": self.me["id"], "name": self.me["name"], "hub": True}]
            out += [{"id": k, "name": v["name"], "hub": False} for k, v in self.nodes.items()
                    if account is None or not self.server_hub or self.owner_of(k) == account]
            if self.multi() and account is not None:
                for m in out:             # 主人：这台共享给了谁，各自能不能用 Codex
                    m["shares"] = dict(self.nodes[m["id"]].get("shares") or {})
                if not own:               # 别的账号共享给它的（#154）
                    out += [{"id": k, "name": v["name"], "hub": False, "shared": True, "share": v["shares"][account]}
                            for k, v in self.nodes.items()
                            if self.owner_of(k) != account and account in (v.get("shares") or {})]
        else:
            out = [dict(m) for m in self.machines_from_hub] or \
                ([] if self.hub.get("server") else [{"id": self.hub["hub_id"], "name": self.hub["hub_name"], "hub": True}]) + \
                [{"id": self.me["id"], "name": self.me["name"], "hub": False}]
        for m in out:
            m["me"] = m["id"] == self.me["id"]
            m["online"] = self.online(m["id"])
        return out

    # ----- 配对（总控） -----

    def new_code(self, account=None):
        if not self.is_hub():
            raise ValueError("这台电脑已经加入了别的电脑，要添加电脑请到那台电脑上操作。")
        self.serve()
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self.lock:
            self.codes = {c: v for c, v in self.codes.items() if v["until"] > time.time()}
            pid = secrets.token_hex(8)        # 网页认进度用（#175），推送里不带配对码本身
            self.codes[code] = {"until": time.time() + CODE_TTL, "tries": 0, "account": account, "pid": pid}
        if self.public_url:                   # 服务器上的总控：给公网地址
            return {"code": code, "pid": pid, "minutes": CODE_TTL // 60, "hosts": [self.public_url], "port": None}
        return {"code": code, "pid": pid, "minutes": CODE_TTL // 60, "hosts": lan_ips(), "port": PEER_PORT}

    def progress(self, body):
        """新电脑上的 join.sh、ccim join 报进度（#175）：配对码对得上才推给网页。"""
        from . import onboard
        with self.lock:
            rec = self.codes.get(str(body.get("code") or ""))
            if not rec or rec["until"] < time.time():
                return
        ev = onboard.progress_event(rec, body)
        if ev:
            self.pf.channel.events.publish(ev)

    def pair(self, body):
        code = str(body.get("code") or "")
        with self.lock:
            live = {c: v for c, v in self.codes.items() if v["until"] > time.time()}
            for v in live.values():
                v["tries"] += 1
            if code not in live or live[code]["tries"] > 10 or live[code].get("used"):
                if all(v["tries"] > 10 for v in live.values()):
                    self.codes = {}
                raise PermissionError("配对码不对或者过期了，在那台电脑的网页里重新生成一个。")
            rec = live[code]
            rec["used"] = True                # 用过了，留到过期：那台电脑接着报的进度还要认（#175）
            account = rec.get("account")
            mid = str(body.get("id") or "")[:20] or "m" + uuid.uuid4().hex[:8]
            if mid == self.me["id"]:
                raise PermissionError("不能加入自己。")
            if self.server_hub and mid in self.nodes and self.owner_of(mid) != (account or OWNER):
                raise PermissionError("这台电脑已经在别的账号下了，先在那个账号里移除它。")
            key = secrets.token_hex(32)
            self.nodes[mid] = {"name": str(body.get("name") or mid)[:40], "key": key, "added": registry.now()}
            if self.server_hub and account and account != OWNER:
                self.nodes[mid]["account"] = account
            _save("peers.json", self.nodes)
        self._emit_machines()
        from . import onboard
        ev = onboard.progress_event(rec, {"step": "done", "name": self.nodes[mid]["name"]})
        if ev:
            self.pf.channel.events.publish(ev)
        return {"hub_id": self.me["id"], "hub_name": self.me["name"], "key": key, "protocol": PROTOCOL,
                "server": self.server_hub}

    def remove(self, mid):
        if not self.is_hub():
            raise ValueError("在总控那台电脑上移除。")
        with self.lock:
            if self.nodes.pop(mid, None) is None:
                raise ValueError("没有这台电脑。")
            _save("peers.json", self.nodes)
            self.boxes.pop(mid, None)
            self.seen.pop(mid, None)
            self.cache.pop(mid, None)
        self._emit_machines()
        return {}

    def leave(self):
        leave()
        return {}

    def multi(self):
        """服务器上的总控打开了多账号（hubserver.accounts_on）。"""
        return bool(self.server_hub and getattr(self.pf, "accounts_on", lambda: False)())

    def guest_of(self, mid, account):
        """服务器上的总控：account 用 mid 是作为访客（别人共享给它的），是就返回 True。"""
        return bool(self.multi() and account and self.owner_of(mid) != account
                    and account in ((self.nodes.get(mid) or {}).get("shares") or {}))

    def share(self, mid, account, opts):
        """主人给 account 在 mid 上开的权限（hubserver.clean_share 的结果）；None = 不共享。"""
        with self.lock:
            n = self.nodes.get(mid)
            if not n:
                raise ValueError("没有这台电脑。")
            sh = n.setdefault("shares", {})
            if opts:
                sh[account] = opts
            else:
                sh.pop(account, None)
            if not sh:
                n.pop("shares", None)
            _save("peers.json", self.nodes)
            self.dirty.add(mid)
            self.dirty.add(f"{mid}|{account}")
        self._emit_machines()
        return {}

    def same_account(self, a, b):
        """服务器上的总控：两台电脑是不是同一个账号的（不同账号的电脑之间不互相调用、不转推送，#154）。"""
        if not self.server_hub:
            return True
        return self.owner_of(a) == self.owner_of(b)

    def _emit_machines(self):
        self.pf.channel.events.publish({"type": "machines"})

    # ----- 转交请求 -----

    def _touched(self, ev):
        """别的电脑上有了变化（正在干活的进度不算）：它的 state 下次要现问。"""
        mid = ev.get("_m")
        if mid and not (ev.get("type") == "progress" and ev.get("state") == "working"):
            self.dirty.add(mid)

    def forward(self, mid, method, path, body=b"", headers=None):
        """把一个接口请求交给另一台电脑执行，返回 (状态码, 内容类型, 内容)。连不上抛 Offline。"""
        if method == "POST":
            self.dirty.add(mid)
        if mid == self.me["id"]:
            return local_request(self.pf, method, path, body, headers)
        if not self.online(mid):
            raise Offline()
        timeout = STATE_TIMEOUT if urlparse(path).path in QUICK else CALL_TIMEOUT
        if self.is_hub():
            return self._to_node(mid, method, path, body, headers, timeout)
        h = {"X-Target": mid, "X-Method": method, "X-Path": path}
        for k in ("X-Name", "Content-Type"):
            if (headers or {}).get(k):
                h[k] = headers[k]
        try:
            status, rh, data = self._hub_request("/peer/call", body, h, timeout + 5)
        except (OSError, http.client.HTTPException):
            raise Offline()
        if status == 503:
            raise Offline()
        return int(rh.get("X-Status") or status), rh.get("X-Ctype") or "application/json", data

    def _to_node(self, mid, method, path, body, headers, timeout):
        rid = uuid.uuid4().hex
        ev, picked = threading.Event(), threading.Event()
        self.waiting[rid] = [ev, None, picked]
        self._box(mid).put({"kind": "req", "rid": rid, "method": method, "path": path,
                            "headers": {k: v for k, v in (headers or {}).items() if k in PASS_HEADERS},
                            "body": base64.b64encode(body or b"").decode()})
        try:
            if not picked.wait(POLL_WAIT + 10):      # 一直没人来取：那台电脑其实已经断了，撤回，免得它回来后又执行一次
                box = self._box(mid)
                with box.mutex:
                    for m in [m for m in box.queue if m.get("rid") == rid]:
                        box.queue.remove(m)
                raise Offline()
            if not ev.wait(timeout):
                raise Offline()
            return self.waiting[rid][1]
        finally:
            self.waiting.pop(rid, None)

    def _box(self, mid):
        with self.lock:
            if mid not in self.boxes:
                self.boxes[mid] = queue.Queue(maxsize=5000)
            return self.boxes[mid]

    # ----- 汇总各台电脑的 state -----

    def merged_state(self, local, account=None):
        out = dict(local)
        me = self.me["id"]
        for k in ("agents", "chats", "projects", "schedules"):
            out[k] = [dict(x, machine=me) for x in local.get(k) or []]
        machines = self.machines(account)
        guest = {m["id"] for m in machines if m.get("shared")}   # 别人共享的电脑：按访客要，缓存也分开（#154）
        key = lambda mid: f"{mid}|{account}" if mid in guest else mid
        hdr = lambda mid: self.guest_headers(account, mid) if mid in guest else None
        extra = {me: {"models": local.get("models"), "defaults": local.get("defaults"), "codex": local.get("codex"),
                      "home": local.get("home"), "ccim": local.get("ccim"), "global": local.get("global") or {}}}
        results = {}

        def fetch(m):
            try:
                status, _, data = self.forward(m["id"], "GET", "/api/state?local=1", b"", hdr(m["id"]))
                if status == 200:
                    s = json.loads(data)
                    if m["id"] in guest and not self.guest_ready(m["id"], s):
                        s = {}
                    results[m["id"]] = s
            except Exception:
                pass

        now = time.time()
        others = [m for m in machines if not m["me"] and m["online"]]
        # 刚操作过、有新动静、或者太久没取的现问；其余用上次取的，后台再更新一下
        now_ = [m for m in others if m["id"] in self.dirty or key(m["id"]) in self.dirty or key(m["id"]) not in self.cache
                or now - self.cache_at.get(key(m["id"]), 0) > STATE_FRESH]
        later = [m for m in others if m not in now_ and now - self.cache_at.get(key(m["id"]), 0) > STATE_REFRESH
                 and key(m["id"]) not in self.refreshing]
        for m in now_:
            if m["id"] in self.dirty:     # 那台电脑有了变化：访客那份也要重取（编号本身就是主人那份的键）
                self.dirty.update(f"{m['id']}|{a}" for a in (self.nodes.get(m["id"]) or {}).get("shares") or {})
            self.dirty.discard(key(m["id"]))
        threads = [threading.Thread(target=fetch, args=(m,)) for m in now_]
        for t in threads:
            t.start()
        for t in threads:
            t.join(STATE_TIMEOUT + 1)
        for m in later:
            self.refreshing.add(key(m["id"]))

            def bg(m=m, k=key(m["id"])):
                try:
                    status, _, data = self.forward(m["id"], "GET", "/api/state?local=1", b"", hdr(m["id"]))
                    if status == 200:
                        s = json.loads(data)
                        if m["id"] in guest and not self.guest_ready(m["id"], s):
                            s = {}
                        self.cache[k], self.cache_at[k] = s, time.time()
                except Exception:
                    pass
                finally:
                    self.refreshing.discard(k)
            threading.Thread(target=bg, daemon=True).start()
        for mid in results:
            self.cache_at[key(mid)] = time.time()
        fresh = {m["id"] for m in others if m not in now_}       # 用的是缓存，也算在线
        changed = False
        for m in machines:
            if m["me"]:
                continue
            s = results.get(m["id"])
            if s:
                self.cache[key(m["id"])] = s
                changed = True
            else:
                s = self.cache.get(key(m["id"]))
                m["online"] = m["online"] and m["id"] in fresh
            if not s:
                continue
            for k in ("agents", "chats", "projects", "schedules"):
                out[k] += [dict(x, machine=m["id"], **({} if m["online"] else {"busy": False})) for x in s.get(k) or []]
            extra[m["id"]] = {"models": s.get("models"), "defaults": s.get("defaults"), "codex": s.get("codex"),
                              "home": s.get("home"), "ccim": s.get("ccim"), **({"guest": s["guest"]} if s.get("guest") else {}),
                              "global": {k: v for k, v in (s.get("global") or {}).items()     # 那台电脑的默认模型（新对话页显示）
                                         if k in ("model", "effort", "codex_model", "codex_effort", "archive_days")}}
        if changed:
            try:
                _save("peer-cache.json", self.cache)
            except OSError:
                pass
        out["chats"].sort(key=lambda r: r.get("last_active") or "", reverse=True)
        out["machines"], out["me"], out["machine_info"] = machines, me, extra
        if self.hub:
            out["hub_name"] = self.hub.get("hub_name")
        return out

    def guest_ready(self, mid, st):
        """共享出去的电脑认不认访客：新版本按访客给的 state 里有 guest。旧版本不认，会把主人的东西全给出去，一律不转（#154）。"""
        ok = "guest" in (st or {})
        self.guest_ok[mid] = ok
        return ok

    def guest_headers(self, account, mid=None):
        """访客的请求转给共享电脑时带的头：账号编号、名字、主人给它的权限（URL 编码，头里只能放 ASCII）。"""
        from urllib.parse import quote
        name = (self.pf.account(account) or {}).get("name") if hasattr(self.pf, "account") else ""
        opts = ((self.nodes.get(mid) or {}).get("shares") or {}).get(account) or {}
        return {"X-Account": account, "X-Account-Name": quote(name or ""),
                "X-Account-Share": quote(json.dumps(opts, ensure_ascii=False))}

    def agent_home(self, aid):
        """这个 Agent 在哪台电脑上（本机的直接认；别的电脑看上次汇总的，没有就现问一遍）。找不到返回 None。"""
        from . import agents
        if agents.get(aid):
            return self.me["id"]
        for again in (False, True):
            if again:
                self.merged_state({"agents": [], "chats": [], "projects": [], "schedules": []})
            for mid, s in list(self.cache.items()):
                if "|" not in mid and any(a.get("id") == aid for a in s.get("agents") or []):
                    return mid
        return None

    # ----- 离线时先存着的消息 -----

    def queue_out(self, mid, method, path, body):
        box = _load("outbox.json", [])
        box.append({"machine": mid, "method": method, "path": path, "body": base64.b64encode(body or b"").decode(),
                    "at": time.time()})
        _save("outbox.json", box)

    def _outbox_loop(self):
        while not self.stopping:
            time.sleep(10)
            box = _load("outbox.json", [])
            if not box:
                continue
            left = []
            for item in box:
                if not self.online(item["machine"]):
                    left.append(item)
                    continue
                try:
                    self.forward(item["machine"], item["method"], item["path"], base64.b64decode(item["body"]),
                                 {"Content-Type": "application/json"})
                except Offline:
                    left.append(item)
                except Exception:
                    log.warning("补发失败", exc_info=True)
            _save("outbox.json", left)

    # ----- 推送 -----

    def _fanout_loop(self):
        """总控：本机产生的、加入的电脑交上来的推送，转给其他电脑（不回给来源）。"""
        q = self.pf.channel.events.subscribe()
        while not self.stopping:
            try:
                ev = q.get(timeout=5)
            except queue.Empty:
                if not self.pf.channel.events.alive(q):
                    q = self.pf.channel.events.subscribe()
                continue
            origin = ev.get("_m") or self.me["id"]
            if ev.get("_a"):                  # 共享电脑上访客的推送：只给服务器上访客的网页
                continue
            for mid in list(self.nodes):
                if mid != origin and self.online(mid) and self.same_account(mid, origin):
                    try:
                        self._box(mid).put_nowait({"kind": "ev", "ev": dict(ev, _m=origin)})
                    except queue.Full:
                        pass

    def _push_loop(self):
        """加入的电脑：本机产生的推送交给总控。"""
        q = self.pf.channel.events.subscribe()
        while not self.stopping and self.hub:
            batch = []
            try:
                batch.append(q.get(timeout=5))
                while len(batch) < 200:
                    batch.append(q.get(timeout=0.15))
            except queue.Empty:
                pass
            if not self.pf.channel.events.alive(q):
                q = self.pf.channel.events.subscribe()
            batch = [e for e in batch if e.get("_m") in (None, self.me["id"])]
            if not batch or not self.online(self.hub["hub_id"]):
                continue
            try:
                self._hub_request("/peer/push", json.dumps(batch, ensure_ascii=False).encode(), {}, 10)
            except Exception:
                pass

    # ----- 加入的电脑：和总控说话 -----

    def _hub_request(self, path, body=b"", headers=None, timeout=30):
        c = _conn(self.hub["url"], timeout)
        ts, nonce = str(int(time.time())), secrets.token_hex(12)
        h = dict(headers or {}, **{"X-Node": self.me["id"], "X-Ts": ts, "X-Nonce": nonce,
                                   "X-Sig": _sign(self.hub["key"], "POST", path, ts, nonce, _digest(body))})
        try:
            c.request("POST", path, body=body or b"", headers=h)
            r = c.getresponse()
            data = r.read()
            rh = {k: v for k, v in r.getheaders()}
        finally:
            c.close()
        if r.status == 401:
            raise PermissionError("总控不认这台电脑了（可能被移除了），重新运行 ccim join。")
        sig = rh.get("X-Sig") or ""
        if not hmac.compare_digest(sig, _sign(self.hub["key"], "resp", nonce, str(r.status), _digest(data))):
            raise http.client.HTTPException("总控的回应签名不对")
        return r.status, rh, data

    def _poll_loop(self):
        fails = 0
        while not self.stopping and self.hub:
            try:
                status, _, data = self._hub_request("/peer/poll", json.dumps({"name": self.me["name"],
                                                                              "protocol": PROTOCOL}).encode(),
                                                    {}, POLL_WAIT + 15)
                if status != 200:
                    raise OSError(f"总控回应 {status}")
                if not self.hub_ok or time.time() - self.hub_ok > ONLINE_WINDOW:
                    log.info("连上总控 %s", self.hub["hub_name"])
                    self._emit_machines()
                self.hub_ok, fails = time.time(), 0
                msg = json.loads(data)
                self.machines_from_hub = msg.get("machines") or self.machines_from_hub
                for m in msg.get("msgs") or []:
                    if m["kind"] == "ev":
                        self._touched(m["ev"])
                        self.pf.channel.events.publish(m["ev"])
                    elif m["kind"] == "req":
                        threading.Thread(target=self._serve_req, args=(m,), daemon=True).start()
            except PermissionError as e:
                log.error("%s", e)
                time.sleep(60)
            except Exception as e:
                fails += 1
                if fails == 3:
                    log.warning("连不上总控 %s：%s", self.hub["url"], e)
                    self._emit_machines()
                time.sleep(min(3 * fails, 15))

    def _serve_req(self, m):
        # 这几个总控只等 STATE_TIMEOUT 秒，过了就当没回应；这边也别再干等，不然网页一直重试、请求越积越多
        timeout = STATE_TIMEOUT if urlparse(m["path"]).path in QUICK else CALL_TIMEOUT
        try:
            status, ctype, data = local_request(self.pf, m["method"], m["path"], base64.b64decode(m["body"]),
                                                m.get("headers"), timeout)
        except Exception as e:
            status, ctype, data = 500, "application/json", json.dumps({"error": f"出错了：{e}"}).encode()
        try:
            self._hub_request("/peer/reply", data, {"X-Rid": m["rid"], "X-Status": str(status), "X-Ctype": ctype}, 60)
        except Exception:
            log.warning("回复总控失败", exc_info=True)


# ---------- 总控对局域网开的端口 ----------

def _handler(peers):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ccim-peer"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, status, body, key=None, nonce="", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if key:
                self.send_header("X-Sig", _sign(key, "resp", nonce, str(status), _digest(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            """新电脑接入（#175）：join.sh 和安装包，不用登录。"""
            from . import onboard
            path = urlparse(self.path).path
            try:
                if path in ("/join.sh", "/peer/join.sh"):
                    base = peers.public_url or "http://" + (self.headers.get("Host") or f"127.0.0.1:{PEER_PORT}")
                    return self._send_raw(200, onboard.script(base), "text/x-shellscript; charset=utf-8")
                if path == "/peer/version":
                    _, name, sha = onboard.wheel()
                    return self._reply(200, {"commit": onboard.commit()[:7], "version": onboard.version(),
                                             "wheel": name, "sha256": sha})
                if path.startswith("/peer/dl/"):
                    f = onboard.wheel_file(path.rsplit("/", 1)[-1])
                    if f:
                        with open(f, "rb") as fh:
                            return self._send_raw(200, fh.read(), "application/octet-stream")
            except Exception:
                log.exception("提供 %s 出错", path)
                return self._reply(500, {"error": "出错了"})
            return self._reply(404, {"error": "not found"})

        def _send_raw(self, status, data, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 160 * 1024 * 1024:
                self.close_connection = True
                return self._reply(413, {"error": "太大了"})
            body = self.rfile.read(n) if n else b""
            path = urlparse(self.path).path
            if path == "/peer/pair":
                try:
                    return self._reply(200, peers.pair(json.loads(body or b"{}")))
                except PermissionError as e:
                    return self._reply(403, {"error": str(e)})
                except ValueError:
                    return self._reply(400, {"error": "请求不对"})
            if path == "/peer/progress":
                try:
                    peers.progress(json.loads(body or b"{}"))
                except ValueError:
                    pass
                return self._reply(200, {})
            mid = self.headers.get("X-Node") or ""
            node = peers.nodes.get(mid)
            ts, nonce, sig = self.headers.get("X-Ts") or "0", self.headers.get("X-Nonce") or "", self.headers.get("X-Sig") or ""
            if not node or not nonce or abs(time.time() - int(ts if ts.isdigit() else 0)) > SKEW or \
                    not hmac.compare_digest(sig, _sign(node["key"], "POST", path, ts, nonce, _digest(body))) or \
                    not peers.nonces.fresh(nonce):
                return self._reply(401, {"error": "unauthorized"})
            key = node["key"]
            peers.seen[mid] = time.time()
            try:
                if path == "/peer/poll":
                    return self._poll(mid, key, nonce, body)
                if path == "/peer/reply":
                    w = peers.waiting.get(self.headers.get("X-Rid") or "")
                    if w:
                        w[1] = (int(self.headers.get("X-Status") or 200), self.headers.get("X-Ctype") or "application/json", body)
                        w[0].set()
                    return self._reply(200, {}, key, nonce)
                if path == "/peer/push":
                    for ev in json.loads(body or b"[]"):
                        peers._touched(dict(ev, _m=mid))
                        peers.pf.channel.events.publish(dict(ev, _m=mid))
                    return self._reply(200, {}, key, nonce)
                if path == "/peer/call":
                    target = self.headers.get("X-Target") or ""
                    method, p = self.headers.get("X-Method") or "GET", self.headers.get("X-Path") or "/"
                    if not p.startswith("/api/"):
                        return self._reply(400, {"error": "bad path"}, key, nonce)
                    if peers.server_hub and (target not in peers.nodes or not peers.same_account(mid, target)):
                        return self._reply(503, {"error": "offline"}, key, nonce)
                    hs = {k: self.headers[k] for k in ("X-Name", "Content-Type") if self.headers.get(k)}
                    try:
                        status, ctype, data = peers.forward(target, method, p, body, hs)
                    except Offline:
                        return self._reply(503, {"error": "offline"}, key, nonce)
                    return self._reply(200, data, key, nonce, {"X-Status": str(status), "X-Ctype": ctype})
            except Exception as e:
                log.exception("处理 %s 出错", path)
                return self._reply(500, {"error": str(e)}, key, nonce)
            return self._reply(404, {"error": "not found"}, key, nonce)

        def _poll(self, mid, key, nonce, body):
            try:
                info = json.loads(body or b"{}")
                if info.get("name") and peers.nodes[mid].get("name") != info["name"]:
                    peers.nodes[mid]["name"] = str(info["name"])[:40]
                    _save("peers.json", peers.nodes)
            except (ValueError, KeyError):
                pass
            first = mid not in peers.boxes
            box = peers._box(mid)
            if first:
                peers._emit_machines()
            msgs = []
            try:
                msgs.append(box.get(timeout=POLL_WAIT))
                while len(msgs) < 200:
                    msgs.append(box.get_nowait())
            except queue.Empty:
                pass
            for m in msgs:                    # 请求被取走了：那边在线，接着等它的回复
                w = peers.waiting.get(m.get("rid") or "")
                if w:
                    w[2].set()
            return self._reply(200, {"machines": peers.machines(peers.owner_of(mid)), "msgs": msgs}, key, nonce)

    return Handler


# ---------- ccim join ----------

def leave():
    """加入的这台：断开总控。"""
    try:
        os.remove(_file("hub.json"))
    except OSError:
        pass


def hub_url(host):
    """ccim join 后面写的地址 → 总控的地址（和 hub.json 里存的一样写法）。"""
    if host.startswith("https://"):
        return host.rstrip("/")
    if ":" not in host.split("//")[-1]:
        host = f"{host}:{PEER_PORT}"
    return (host if host.startswith("http") else "http://" + host).rstrip("/")


def check_joined():
    """已经加入的总控还认不认这台电脑：True 认 / False 不认了 / None 连不上。"""
    p = Peers.__new__(Peers)
    p.hub, p.me = _load("hub.json", None), identity()
    try:
        p._hub_request("/peer/push", b"[]", timeout=15)
        return True
    except PermissionError:
        return False
    except Exception:
        return None


def report(url, code, step, **kw):
    """告诉总控这台电脑接入到哪一步了（网页上实时显示），连不上就算了。"""
    try:
        c = _conn(url, 5)
        try:
            c.request("POST", "/peer/progress", body=json.dumps(dict(kw, code=code, step=step)).encode(),
                      headers={"Content-Type": "application/json"})
            c.getresponse().read()
        finally:
            c.close()
    except Exception:
        pass


def join(host, code):
    """加入另一台电脑或服务器上的总控。返回总控的名字。https:// 开头的是服务器（端口默认 443）。"""
    url = hub_url(host)
    me = identity()
    u = urlparse(url)
    c = _conn(url, 15)
    try:
        c.request("POST", "/peer/pair", body=json.dumps({"code": code, "id": me["id"], "name": me["name"]}).encode(),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
    except OSError as e:
        where = "服务器" if u.scheme == "https" else "那台电脑"
        raise SystemExit(f"连不上 {u.hostname}（{e}）。确认地址对、{where}上的 ccim 在运行"
                         + ("，而且两台电脑在同一个网络里、系统防火墙允许 ccim 接收连接。" if u.scheme != "https" else "。"))
    finally:
        c.close()
    if r.status != 200:
        raise SystemExit(data.get("error") or f"加入失败（{r.status}）")
    _save("hub.json", {"url": url, "hub_id": data["hub_id"], "hub_name": data["hub_name"], "key": data["key"],
                       "server": bool(data.get("server")), "joined": registry.now()})
    return data["hub_name"]
