"""放在服务器上的总控（#16）：`ccim hub`。

只做三件事：网页（和 Mac 上的是同一个页面）、登录、把请求转给对应的 Mac。不跑 Claude、不存对话和代码。
Mac 们照旧用 peers.py 的方式主动连过来（/peer/* 由 Peers 自己的服务处理，这里不管）。
部署时前面放 nginx：HTTPS（IP 证书）、把 /peer/ 转给 PEER_PORT、其余转给这里，所以两个服务都只听 127.0.0.1。

登录：密码（scrypt）+ 手机验证器动态码（TOTP，RFC 6238）。第一次打开要用设置链接（setup-token 文件里的一次性口令）设密码、绑验证器。
登录后发一个 Cookie（HttpOnly、Secure、SameSite=Strict，30 天），服务器只存它的摘要。接口还要带 X-CCIM 头（别的网站发不过来）。
账号（#154）：accounts.json，主人是 OWNER（老的 auth.json 迁过来，账号名 admin；登录时账号名留空也算主人），
主人在「设置 → 账号」建别的账号：账号名 + 密码（默认生成）、有效期、要不要动态码（要的话给对方一次性链接去绑验证器）。
电脑归加入它的账号（peers.json 的 account），网页、转交、推送都只给这个账号的电脑；电脑之间的调用、推送也只在同一个账号内（peers.py）。
多账号默认关闭（个人用就和以前一样：只有主人，密码 + 动态码），`ccim hub accounts on` 打开（settings.json 的 accounts）。
主人还能按电脑给某个账号开权限（peers 的 shares：能用哪些 Agent、能不能自己建、能不能用 Codex、共享文件夹），
转过去的请求带 X-Account*，那台电脑按它圈住对方（share.py）。
连续输错：同一个地址 15 分钟里错 5 次锁 15 分钟；所有地址一小时里错 20 次全部锁 30 分钟。
从这里转出去的请求，到了 Mac 那边都带 X-From-Peer：开机密码、选文件夹弹窗、在访达里显示这些 Mac 会拒绝。
"""
import base64, hashlib, hmac, html, json, logging, os, queue, secrets, struct, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import registry
from .peers import OWNER

log = logging.getLogger("ccim.hub")

WEB_PORT = 8977
SESSION_DAYS = 30
INVITE_DAYS = 7
OWNER_NAME = "admin"
PW_CHARS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"   # 去掉了 0 O 1 l I，抄的时候不会看错


class NeedCode(PermissionError):
    """密码对了，这个账号还要动态码。"""


def new_password():
    return "-".join("".join(secrets.choice(PW_CHARS) for _ in range(4)) for _ in range(3))


def clean_share(opts):
    """主人给某个账号在某台电脑上开的权限；None / False = 不共享。"""
    if not opts:
        return None
    o = opts if isinstance(opts, dict) else {}
    return {"agents": [str(x) for x in o.get("agents") or [] if x][:200], "own": bool(o.get("own", True)),
            "codex": bool(o.get("codex")), "dir": (str(o.get("dir") or "").strip() or None)}
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
ISSUER = "ccim"


def home():
    d = os.path.join(registry.HOME, "hub")
    os.makedirs(d, exist_ok=True)
    return d


def _f(name):
    return os.path.join(home(), name)


def _load(name, default):
    try:
        with open(_f(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, data):
    tmp = _f(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _f(name))


# ---------- 密码和动态码 ----------

def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return base64.b64encode(salt).decode(), base64.b64encode(h).decode()


def check_password(pw, rec):
    try:
        salt = base64.b64decode(rec["salt"])
        _, h = hash_password(pw, salt)
        return hmac.compare_digest(h, rec["hash"])
    except Exception:
        return False


def totp_at(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    d = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = d[-1] & 15
    return "%06d" % ((struct.unpack(">I", d[o:o + 4])[0] & 0x7fffffff) % 10 ** 6)


def totp_check(secret, code, last=0):
    """对的话返回用掉的那个计数（同一个码不能用两次），不对返回 None。前后各容 30 秒。"""
    code = "".join(c for c in str(code or "") if c.isdigit())
    now = int(time.time()) // 30
    for c in (now - 1, now, now + 1):
        if c > last and hmac.compare_digest(totp_at(secret, c), code):
            return c
    return None


def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    return qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).to_string(encoding="unicode")


# ---------- 总控 ----------

class Hub:
    def __init__(self, public_url=None, port=WEB_PORT):
        from .platform import Events
        self.public_url = (public_url or "").rstrip("/")
        self.port = port
        self.home = home()
        self.channel = type("C", (), {})()
        self.channel.events = Events()
        from .peers import Peers
        self.peers = Peers(self, server=True, public_url=self.public_url)
        self.lock = threading.Lock()
        self.fails = {}                       # 地址 → [失败时间]
        self.all_fails = []
        self.pending_totp = {}                # 设置时：口令 → 新的验证器密钥（还没确认）

    # ----- 账号（#154） -----

    def accounts_on(self):
        """多账号打开了没有。关着时只有主人能登录，没有账号管理、共享。"""
        return bool(_load("settings.json", {}).get("accounts"))

    def accounts(self):
        acc = _load("accounts.json", None)
        if acc is None:                       # 只有一个账号时的 auth.json：迁成主人（留着 auth.json，装回旧版本还能登录）
            a = _load("auth.json", None)
            acc = {OWNER: dict(a, name=OWNER_NAME, role=OWNER)} if a else {}
            if acc:
                _save("accounts.json", acc)
        return acc

    def _save_accounts(self, acc):
        _save("accounts.json", acc)

    @staticmethod
    def active(a):
        return bool(a and a.get("password") and not a.get("disabled")
                    and not (a.get("expires") and a["expires"] < time.time()))

    def account(self, aid):
        return self.accounts().get(aid or "")

    def account_view(self, aid):
        a = self.account(aid) or {}
        return {"id": aid, "name": a.get("name") or "", "owner": aid == OWNER}

    def _by_name(self, acc, name):
        name = (name or "").strip().lower()
        if not name:
            return OWNER if OWNER in acc else None
        return next((aid for aid, a in acc.items() if (a.get("name") or "").lower() == name), None)

    # ----- 登录 -----

    def auth(self):
        return self.account(OWNER)

    def setup_token(self):
        """主人还没设置密码时的一次性设置口令（没有就生成），设置好了返回 None。"""
        if self.auth():
            return None
        t = _load("setup.json", {}).get("token")
        if not t:
            t = secrets.token_urlsafe(24)
            _save("setup.json", {"token": t})
        return t

    def _link(self, token):
        return f"{self.public_url or f'http://127.0.0.1:{self.port}'}/setup?token={token}"

    def setup_link(self):
        t = self.setup_token()
        return self._link(t) if t else None

    def locked(self, ip):
        now = time.time()
        with self.lock:
            self.all_fails = [t for t in self.all_fails if now - t < 3600]
            mine = self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < 900]
            if len(self.all_fails) >= 20 and now - self.all_fails[-1] < 1800:
                return True
            return len(mine) >= 5

    def failed(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())
            self.all_fails.append(time.time())

    def login(self, ip, name, pw, code):
        """账号名 + 密码，账号要动态码的再加动态码（没给就抛 NeedCode，不算输错）。"""
        if self.locked(ip):
            raise PermissionError("错的次数太多了，等一刻钟再试。")
        acc = self.accounts()
        if not acc:
            raise PermissionError("还没设置密码。")
        with self.lock:
            aid = self._by_name(acc, name) if self.accounts_on() else OWNER
            a = acc.get(aid) if aid else None
            if a and self.active(a) and check_password(pw or "", a["password"]):
                if not a.get("totp"):
                    return self.new_session(aid)
                if not str(code or "").strip():
                    raise NeedCode("输入手机验证器上的 6 位动态码。")
                used = totp_check(a["totp"], code, a.get("totp_last", 0))
                if used is not None:
                    a["totp_last"] = used
                    self._save_accounts(acc)
                    return self.new_session(aid)
        self.failed(ip)
        if a and not self.active(a) and a.get("password") and check_password(pw or "", a["password"]):
            raise PermissionError("这个账号已停用或到期了，请联系开账号的人。")
        raise PermissionError("账号、密码或动态码不对。")

    def new_session(self, aid=OWNER):
        tok = secrets.token_urlsafe(32)
        s = _load("sessions.json", {})
        now = time.time()
        s = {k: v for k, v in s.items() if (v if isinstance(v, (int, float)) else v.get("exp", 0)) > now}
        s[hashlib.sha256(tok.encode()).hexdigest()] = {"exp": now + SESSION_DAYS * 86400, "acct": aid}
        _save("sessions.json", s)
        return tok

    def session_account(self, tok):
        """登录着的账号编号；没登录、过期、账号停用了返回 None。"""
        if not tok:
            return None
        v = _load("sessions.json", {}).get(hashlib.sha256(tok.encode()).hexdigest())
        if isinstance(v, (int, float)):       # 只有一个账号时的登录
            v = {"exp": v, "acct": OWNER}
        if not v or v.get("exp", 0) <= time.time():
            return None
        if v["acct"] != OWNER and not self.accounts_on():
            return None
        return v["acct"] if self.active(self.account(v["acct"])) else None

    def session_ok(self, tok):
        return bool(self.session_account(tok))

    def logout(self, tok):
        s = _load("sessions.json", {})
        s.pop(hashlib.sha256((tok or "").encode()).hexdigest(), None)
        _save("sessions.json", s)

    def _drop_sessions(self, aid):
        s = _load("sessions.json", {})
        _save("sessions.json", {k: v for k, v in s.items() if not (isinstance(v, dict) and v.get("acct") == aid)})

    def _setup_target(self, token):
        """设置链接对应的账号编号：主人第一次设置，或者给某个账号绑验证器。不对返回 None。"""
        if not token:
            return None
        if token == self.setup_token():
            return OWNER
        h = hashlib.sha256(token.encode()).hexdigest()
        for aid, a in self.accounts().items():
            if a.get("invite") and hmac.compare_digest(a["invite"], h) and a.get("invite_until", 0) > time.time() \
                    and not a.get("disabled"):
                return aid
        return None

    def setup_begin(self, token):
        aid = self._setup_target(token)
        if not aid:
            raise PermissionError("链接不对、过期了或者已经用过了。")
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        self.pending_totp[token] = secret
        a = self.account(aid) or {}
        name = a.get("name") if aid != OWNER else ""
        label = quote(f"{ISSUER}:{name or urlparse(self.public_url).hostname or 'hub'}")
        uri = f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}"
        return {"secret": secret, "qr": qr_svg(uri), "name": name or "", "bind": bool(a.get("password"))}

    def setup_finish(self, token, pw, code):
        """主人第一次设置：密码 + 验证器。给已有账号绑验证器（bind）：不改密码，只要动态码。"""
        aid = self._setup_target(token)
        if not aid or token not in self.pending_totp:
            raise PermissionError("链接不对、过期了或者已经用过了。")
        acc = self.accounts()
        bind = bool((acc.get(aid) or {}).get("password"))
        if not bind and len(pw or "") < 10:
            raise ValueError("密码至少 10 位。")
        secret = self.pending_totp[token]
        used = totp_check(secret, code)
        if used is None:
            raise ValueError("动态码不对：看看手机上的时间对不对，再输一次。")
        with self.lock:
            acc = self.accounts()
            a = acc.get(aid) or {"name": OWNER_NAME, "role": OWNER}
            if not bind:
                salt, h = hash_password(pw)
                a["password"] = {"salt": salt, "hash": h}
            a.update({"totp": secret, "totp_last": used, "set_at": registry.now()})
            a.pop("invite", None)
            a.pop("invite_until", None)
            acc[aid] = a
            self._save_accounts(acc)
        if aid == OWNER:
            try:
                os.remove(_f("setup.json"))
            except OSError:
                pass
        self.pending_totp.pop(token, None)
        return self.new_session(aid)

    # ----- 主人管理账号 -----

    def account_list(self):
        now = time.time()
        acc = self.accounts()
        mine = self.peers.machines(OWNER, own=True)
        out = []
        for aid, a in acc.items():
            if aid == OWNER:
                continue
            state = "disabled" if a.get("disabled") else "expired" if a.get("expires") and a["expires"] < now else "ok"
            out.append({"id": aid, "name": a.get("name") or "", "state": state, "created": a.get("created"),
                        "expires": a.get("expires"), "totp": bool(a.get("totp")), "totp_pending": bool(a.get("invite")),
                        "machines": [m["name"] for m in self.peers.machines(aid, own=True)],
                        "shares": {m["id"]: (m.get("shares") or {})[aid] for m in mine if aid in (m.get("shares") or {})}})
        return {"accounts": out, "me": (acc.get(OWNER) or {}).get("name") or OWNER_NAME}

    def _bind_link(self, a):
        tok = secrets.token_urlsafe(24)
        a["invite"] = hashlib.sha256(tok.encode()).hexdigest()
        a["invite_until"] = time.time() + INVITE_DAYS * 86400
        return {"link": self._link(tok), "days": INVITE_DAYS}

    def account_save(self, aid, f):
        """主人新建（aid 空）或修改账号。f：name、password（新建不填就生成）、expires（时间戳，0 / 空 = 一直有效）、
        disabled、totp（False 关掉；True 且还没绑：生成绑验证器的链接）、shares {电脑: 权限}。返回 id，有新密码、绑定链接也带上。"""
        f = f if isinstance(f, dict) else {}
        out = {}
        with self.lock:
            acc = self.accounts()
            if aid and aid not in acc:
                raise ValueError("没有这个账号。")
            a = acc.get(aid) or {"role": "member", "created": registry.now()}
            if "name" in f or not aid:
                name = " ".join(str(f.get("name") or "").split())[:20]
                if not name:
                    raise ValueError("写个账号名。")
                if any(c in name for c in " /\\:"):
                    raise ValueError("账号名里不能有空格和 / \\ :")
                other = self._by_name(acc, name)
                if other and other != aid:
                    raise ValueError("已经有叫这个名字的账号了。")
                a["name"] = name
            pw = f.get("password")
            if not aid and not pw:
                pw = new_password()
            if pw:
                if len(pw) < 8:
                    raise ValueError("密码至少 8 位。")
                salt, h = hash_password(pw)
                a["password"] = {"salt": salt, "hash": h}
                out["password"] = pw
            if aid != OWNER:
                if "expires" in f:
                    if f["expires"]:
                        a["expires"] = float(f["expires"])
                    else:
                        a.pop("expires", None)
                if "disabled" in f:
                    a["disabled"] = bool(f["disabled"])
                if f.get("totp") is False:
                    for k in ("totp", "totp_last", "invite", "invite_until"):
                        a.pop(k, None)
                elif f.get("totp") and not a.get("totp"):
                    out["bind"] = self._bind_link(a)
            if not aid:
                aid = "a" + secrets.token_hex(4)
            acc[aid] = a
            self._save_accounts(acc)
        if aid != OWNER and isinstance(f.get("shares"), dict):
            mine = {m["id"] for m in self.peers.machines(OWNER, own=True)}
            for mid, opts in f["shares"].items():
                if mid not in mine:
                    raise ValueError("没有这台电脑。")
                self.peers.share(mid, aid, clean_share(opts))
        if pw and aid != OWNER or a.get("disabled"):
            self._drop_sessions(aid)          # 改了密码、停用：已登录的都要重新登录
        self.channel.events.publish({"type": "machines"})
        return dict(out, id=aid)

    def account_delete(self, aid):
        if aid == OWNER:
            raise ValueError("不能删除自己。")
        with self.lock:
            acc = self.accounts()
            if acc.pop(aid, None) is None:
                raise ValueError("没有这个账号。")
            self._save_accounts(acc)
        self._drop_sessions(aid)
        for m in self.peers.machines(aid, own=True):
            try:
                self.peers.remove(m["id"])
            except ValueError:
                pass
        for m in self.peers.machines(OWNER, own=True):
            if aid in (m.get("shares") or {}):
                self.peers.share(m["id"], aid, None)
        return {}

    # ----- 接口 -----

    def allowed(self, aid):
        """这个账号能看、能操作的电脑。"""
        return {m["id"] for m in self.peers.machines(aid)}

    def visible(self, aid, ev):
        """推送给这个账号的网页：只要它能用的电脑上来的（电脑列表变了的通知谁都给）。
        共享的电脑（#154）：访客只收标了自己（_a）的，和 Skill、知识库有变化这类不属于哪个对话的；主人收不到访客的。"""
        if ev.get("type") == "pairing":           # 新电脑接入的进度（#175）：只给生成配对码的那个账号
            return (ev.get("_acct") or OWNER) == aid
        mid = ev.get("_m")
        if not mid:
            return True
        if mid not in self.allowed(aid):
            return False
        if self.peers.guest_of(mid, aid):
            return ev.get("_a") == aid or (not ev.get("_a") and not ev.get("chat") and ev.get("type") in ("skills", "kb"))
        return not ev.get("_a")

    def default_machine(self, aid=OWNER):
        ms = self.peers.machines(aid)
        on = [m for m in ms if m["online"]]
        return (on or ms or [{"id": None}])[0]["id"]

    def state(self, aid=OWNER):
        out = self.peers.merged_state({"agents": [], "chats": [], "projects": [], "schedules": []}, account=aid)
        out["me"] = self.default_machine(aid)
        out["hub"] = {"name": self.peers.me["name"], "url": self.public_url}
        out["account"] = dict(self.account_view(aid), multi=self.accounts_on())
        try:                                  # 服务器每次合并都自动部署 main，它的版本就是最新版（#37）
            from . import upgrade
            out["hub"]["commit"] = upgrade.info().get("commit") or None
        except Exception:
            pass
        for k in ("global", "defaults", "models", "codex", "home"):   # 页面上这些按「当前那台」取
            info = (out.get("machine_info") or {}).get(out["me"]) or {}
            out.setdefault(k, info.get(k) or ({} if k in ("global", "defaults", "models") else None))
        g = ((out.get("machine_info") or {}).get(out["me"]) or {}).get("global") or {}
        out["global"] = {k: g[k] for k in ("archive_days",) if g.get(k)}   # 默认模型各台电脑自己的，服务器上不显示
        return out

    def serve(self):
        self.peers.start()
        server = ThreadingHTTPServer(("127.0.0.1", self.port), _handler(self))
        server.daemon_threads = True
        log.info("总控网页：127.0.0.1:%d（前面用 nginx 做 HTTPS）", self.port)
        link = self.setup_link()
        if link:
            log.info("还没设置登录密码，打开这个链接设置（只能用一次）：%s", link)
        server.serve_forever()


# ---------- HTTP ----------

LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="ccim"><meta name="theme-color" content="#F1F2EE">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>ccim</title><style>
:root{--paper:#F1F2EE;--panel:#fff;--line:#DCDFD8;--ink:#1D222B;--muted:#6C7169;--qing:#2B4C6F;--bad:#B5402F}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);color:var(--ink);
font:15px/1.6 -apple-system,"PingFang SC","Hiragino Sans GB",sans-serif}
.box{width:min(380px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:28px 26px}
h1{font:600 22px "Songti SC","STSong",serif;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px;font-size:13.5px}
label{display:block;font-size:13px;margin:14px 0 6px}input{width:100%;padding:10px 12px;border:1px solid #C9CDC4;border-radius:10px;font:inherit;font-size:16px;outline:none}
input:focus{border-color:var(--qing)}button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:10px;background:var(--qing);color:#fff;font:inherit;cursor:pointer}
.err{color:var(--bad);font-size:13px;min-height:20px;margin-top:10px}.qr{width:200px;margin:6px auto}.qr svg{width:100%;height:auto}
code{font:12px "SF Mono",Menlo,monospace;word-break:break-all;color:var(--muted)}
</style></head><body><div class="box">__BODY__</div><script>
async function post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"X-CCIM":"1","Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || "出错了"); return d;
}
__SCRIPT__
</script></body></html>"""

LOGIN_BODY = """<h1>ccim</h1><p>登录后使用你的 Agent。</p>
<form id="f"><div id="nmbox"><label for="nm">账号</label><input id="nm" autocomplete="username" autocapitalize="off"></div>
<label for="pw">密码</label><input id="pw" type="password" autocomplete="current-password">
<div id="cb" hidden><label for="code">手机验证器上的 6 位动态码</label><input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"></div>
<button>登录</button><div class="err" id="err"></div></form>"""
LOGIN_SCRIPT = """(nmbox.hidden ? pw : nm).focus();
document.getElementById("f").onsubmit = async e => { e.preventDefault();
  const r = await fetch("/auth/login", {method: "POST", headers: {"X-CCIM": "1", "Content-Type": "application/json"},
    body: JSON.stringify({name: nm.value, password: pw.value, code: code.value})});
  const d = await r.json().catch(() => ({}));
  if (r.ok) { location.href = "/"; return; }
  if (d.need_code) { cb.hidden = false; err.textContent = ""; code.focus(); return; }
  err.textContent = d.error || "出错了"; code.value = ""; };"""

SETUP_BODY = """<h1>设置登录</h1><p id="lead">只需要设置一次。以后登录要密码和手机验证器上的动态码。</p>
<div id="qrbox"><p>准备中…</p></div>
<form id="f"><div id="pwbox"><label for="pw">设一个密码（至少 10 位）</label><input id="pw" type="password" autocomplete="new-password">
<label for="pw2">再输一次</label><input id="pw2" type="password" autocomplete="new-password"></div>
<label for="code">验证器里显示的 6 位动态码</label><input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6">
<button>完成设置</button><div class="err" id="err"></div></form>"""
SETUP_SCRIPT = """const token = new URLSearchParams(location.search).get("token") || "";
let bind = false;
post("/auth/setup/begin", {token}).then(d => {
  bind = d.bind;
  if (bind) { document.querySelector("h1").textContent = `${d.name}，绑定验证器`; lead.textContent = "绑好后，登录时除了账号密码，还要输验证器上的动态码。"; pwbox.hidden = true; }
  qrbox.innerHTML = `<label>用手机上的验证器（如 Google Authenticator、微软验证器、1Password）扫这个码</label>
  <div class="qr">${d.qr}</div><p>扫不了的话，手动输入：<code>${d.secret}</code></p>`; }).catch(x => { qrbox.innerHTML = ""; err.textContent = x.message; });
document.getElementById("f").onsubmit = async e => { e.preventDefault();
  if (!bind && pw.value !== pw2.value) { err.textContent = "两次密码不一样"; return; }
  try { await post("/auth/setup/finish", {token, password: pw.value, code: code.value}); location.href = "/"; }
  catch (x) { err.textContent = x.message; } };"""


def _page(body, script):
    return LOGIN_PAGE.replace("__BODY__", body).replace("__SCRIPT__", script).encode()


def _handler(hub):
    from .peers import Offline

    class Handler(BaseHTTPRequestHandler):
        server_version = "ccim-hub"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _ip(self):
            return self.headers.get("X-Real-IP") or self.client_address[0]

        def _https(self):
            return self.headers.get("X-Forwarded-Proto") == "https"

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            ok = {"127.0.0.1", "localhost"}
            if hub.public_url:
                ok.add(urlparse(hub.public_url).hostname)
            return host in ok

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ccim_hub":
                    return v
            return None

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _set_cookie(self, tok, age=SESSION_DAYS * 86400):
            secure = "; Secure" if self._https() else ""
            return {"Set-Cookie": f"ccim_hub={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"}

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 110 * 1024 * 1024:
                self.close_connection = True
                raise ValueError("太大了")
            return self.rfile.read(n) if n else b""

        def _authed(self):
            """登录着的账号编号，没登录返回 None。"""
            self.acct = hub.session_account(self._cookie())
            return self.acct

        def _machine(self, q):
            """这次请求要的电脑：不是这个账号能用的返回 None。"""
            mid = self.headers.get("X-Machine") or q.get("machine") or hub.default_machine(self.acct)
            return mid if mid in hub.allowed(self.acct) else None

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            from .platform import static_file
            st = static_file(u.path)
            if st:
                return self._send(200, st[0], st[1])
            if u.path == "/join.sh":                 # 新电脑一条命令接入（#175），不用登录；安装包在 /peer/dl/
                from . import onboard
                return self._send(200, onboard.script(hub.public_url or "https://" + (self.headers.get("Host") or "")),
                                  "text/x-shellscript; charset=utf-8")
            if u.path == "/setup":
                return self._send(200, _page(SETUP_BODY, SETUP_SCRIPT), "text/html; charset=utf-8")
            if u.path in ("/", "/index.html"):
                if not self._authed():
                    if hub.setup_token():
                        return self._send(200, _page("<h1>ccim</h1><p>还没设置登录。用服务器上 <code>ccim hub setup-link</code> 给出的链接打开。</p>", ""),
                                          "text/html; charset=utf-8")
                    body = LOGIN_BODY if hub.accounts_on() else LOGIN_BODY.replace('<div id="nmbox">', '<div id="nmbox" hidden>')
                    return self._send(200, _page(body, LOGIN_SCRIPT), "text/html; charset=utf-8")
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authed():
                return self._send(401, {"error": "登录过期了，刷新页面重新登录。"})
            if u.path == "/api/events":
                return self._events()
            if u.path != "/api/file" and self.headers.get("X-CCIM") != "1":
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/state" and not (q.get("local") and self.headers.get("X-Machine")):   # 带 local 的是要某一台自己的（#140）
                return self._send(200, hub.state(self.acct))
            if u.path == "/api/accounts":
                return self._send(200, hub.account_list()) if self.acct == OWNER and hub.accounts_on() \
                    else self._send(403, {"error": "forbidden"})
            if u.path == "/api/file":
                mid = self._machine(q)
                if not mid:
                    return self._send(503 if not hub.allowed(self.acct) else 404, {"error": "还没有电脑连上来。"})
                if hub.peers.guest_of(mid, self.acct) and not self._guest_ready(mid):
                    return self._send(409, {"error": "那台电脑上的 ccim 版本较旧，请电脑的主人先升级。"})
                from .filesend import serve_peer
                return serve_peer(self, hub.peers, mid, {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                                         "X-Content-Type-Options": "nosniff"},
                                  hub.peers.guest_headers(self.acct, mid) if hub.peers.guest_of(mid, self.acct) else None)
            return self._forward(b"", q)

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-CCIM") != "1":
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            try:
                body = self._body()
            except ValueError as e:
                return self._send(413, {"error": str(e)})
            if u.path.startswith("/auth/"):
                return self._auth(u.path, body)
            if not self._authed():
                return self._send(401, {"error": "登录过期了，刷新页面重新登录。"})
            try:
                if u.path.startswith("/api/accounts"):
                    return self._accounts(u.path, body)
                if u.path == "/api/peers/code":
                    return self._send(200, hub.peers.new_code(self.acct))
                if u.path == "/api/peers/remove":
                    mid = json.loads(body or b"{}").get("id")
                    if mid not in {m["id"] for m in hub.peers.machines(self.acct, own=True)}:
                        raise ValueError("没有这台电脑。")
                    return self._send(200, hub.peers.remove(mid))
                if u.path in ("/api/peers/leave", "/api/pick", "/api/reveal") or u.path == "/api/global" and not self.headers.get("X-Machine"):
                    return self._send(400, {"error": "在服务器上不能这样做。"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if u.path == "/api/chat/secret":       # 开机密码不经过服务器
                try:
                    b = json.loads(body or b"{}")
                except ValueError:
                    b = {}
                if b.get("password"):
                    return self._send(400, {"error": "开机密码不能经过服务器，选「在那台电脑上弹授权窗口」。"})
            return self._forward(body, {k: v[0] for k, v in parse_qs(u.query).items()})

        def _auth(self, path, body):
            try:
                b = json.loads(body or b"{}")
            except ValueError:
                b = {}
            try:
                if path == "/auth/login":
                    try:
                        tok = hub.login(self._ip(), b.get("name"), b.get("password"), b.get("code"))
                    except NeedCode as e:
                        return self._send(401, {"error": str(e), "need_code": True})
                    return self._send(200, {}, extra=self._set_cookie(tok))
                if path == "/auth/logout":
                    hub.logout(self._cookie())
                    return self._send(200, {}, extra=self._set_cookie("", 0))
                if path == "/auth/setup/begin":
                    return self._send(200, hub.setup_begin(b.get("token")))
                if path == "/auth/setup/finish":
                    tok = hub.setup_finish(b.get("token"), b.get("password"), b.get("code"))
                    return self._send(200, {}, extra=self._set_cookie(tok))
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def _guest_ready(self, mid):
            """访客要用的共享电脑认不认访客：没问过就先问一次 state。"""
            if mid not in hub.peers.guest_ok:
                try:
                    st, _, data = hub.peers.forward(mid, "GET", "/api/state?local=1", b"", hub.peers.guest_headers(self.acct, mid))
                    hub.peers.guest_ready(mid, json.loads(data) if st == 200 else {})
                except Exception:
                    return False
            return hub.peers.guest_ok.get(mid, False)

        def _accounts(self, path, body):
            if self.acct != OWNER or not hub.accounts_on():
                return self._send(403, {"error": "forbidden"})
            b = json.loads(body or b"{}")
            if path == "/api/accounts/save":
                return self._send(200, hub.account_save(b.get("id"), b.get("fields") or {}))
            if path == "/api/accounts/delete":
                return self._send(200, hub.account_delete(b.get("id")))
            return self._send(404, {"error": "not found"})

        def _forward(self, body, q):
            mid = self._machine(q)
            if not mid:
                if not hub.allowed(self.acct):
                    return self._send(503, {"error": "还没有电脑连上来。"})
                return self._send(404, {"error": "没有这台电脑。"})
            name = next((m["name"] for m in hub.peers.machines() if m["id"] == mid), "那台电脑")
            hs = {k: self.headers[k] for k in ("X-Name", "Content-Type") if self.headers.get(k)}
            if hub.peers.guest_of(mid, self.acct):     # 别人共享给它的电脑：告诉那台电脑是谁（#154）
                if not self._guest_ready(mid):
                    return self._send(409, {"error": f"「{name}」上的 ccim 版本较旧，请电脑的主人先升级。"})
                hs.update(hub.peers.guest_headers(self.acct, mid))
            try:
                status, ctype, data = hub.peers.forward(mid, self.command, self.path, body, hs)
            except Offline:
                path = urlparse(self.path).path
                if self.command == "POST" and path in ("/api/chat/send", "/api/chat/mention") and "X-Account" not in hs:
                    hub.peers.queue_out(mid, "POST", self.path, body)
                    return self._send(200, {"queued": True, "machine": name})
                return self._send(503, {"error": f"「{name}」现在离线，等它连上再试。"})
            except Exception as e:
                log.exception("转交出错")
                return self._send(502, {"error": f"出错了：{e}"})
            self._send(status, data, ctype)

        def _events(self):
            q = hub.channel.events.subscribe()
            acct = self.acct
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")   # nginx 不要攒着
                self.end_headers()
                self.wfile.write(b": hi\n\n")
                self.wfile.flush()
                while hub.channel.events.alive(q):
                    try:
                        ev = q.get(timeout=15)
                        if not hub.visible(acct, ev):
                            continue
                        data = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        data = ": ping\n\n"
                    self.wfile.write(data.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.channel.events.unsubscribe(q)
                self.close_connection = True

    return Handler


def run(public_url=None, port=WEB_PORT, peer_port=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%m-%d %H:%M:%S")
    from . import peers
    if peer_port:
        peers.PEER_PORT = peer_port
    Hub(public_url, port).serve()
