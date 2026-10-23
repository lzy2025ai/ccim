"""个人微信渠道：腾讯官方的微信 ClawBot 接口（iLink，ilinkai.weixin.qq.com）。

扫码登录拿到 bot_token；收消息是长轮询 getupdates（不需要公网地址），游标 get_updates_buf 要存盘，
丢了会重放旧消息。回复要带上对方最近一条消息里的 context_token。图片、文件走微信 CDN，内容用 AES-128-ECB 加密。
协议没有公开文档，做法参考 NousResearch/hermes-agent 的 weixin 接入（gateway/platforms/weixin.py）。

和飞书的差别：只有私聊（ClawBot 拉不进普通群）；消息发出去改不了，所以没有卡片，
干活时显示「正在输入」，审批发一条文字、回复 y / n；没有表情回应。
"""
import asyncio, base64, hashlib, json, logging, mimetypes, os, re, secrets, time, uuid
from urllib.parse import quote
from urllib.request import Request, urlopen

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .. import registry
from .base import Channel, Incoming, inbox_path, split_text

log = logging.getLogger("ccim.weixin")

BASE = "https://ilinkai.weixin.qq.com"
CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.2.0"
CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)
POLL_TIMEOUT = 35          # 长轮询服务端最多挂这么多秒，响应里的 longpolling_timeout_ms 会覆盖它
API_TIMEOUT = 15
FILE_TIMEOUT = 120
TEXT_LIMIT = 1800          # 单条消息的字数上限（iLink 大约 2000 字），超过就拆成几条
FILE_LIMIT = 100 * 1024 * 1024
EXPIRED = -14              # 登录失效，要重新扫码
RATE_LIMIT = -2
RETRY_WAITS = (1, 3)
TICKET_TTL = 600           # 「正在输入」要用的 typing_ticket 缓存多久
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE = 1, 2, 3
MSG_USER, MSG_BOT = 1, 2
EXPIRED_TIP = "微信登录已失效，运行 ccim weixin login 重新扫码（登录了好几个微信号时写上名字）"


# ---------- 扫码登录 ----------

def _get(base, endpoint, timeout=API_TIMEOUT):
    req = Request(f"{base}/{endpoint}", headers={"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _print_qr(url):
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.make(fit=True)
        q.print_ascii(invert=True)
    except Exception:
        pass


def show_in_terminal(url):
    print("\n用微信扫码，确认后就能在微信里和它对话：\n")
    _print_qr(url)
    print(f"\n扫不了的话，在手机微信里打开这个链接：\n{url}\n")


def show_qr(url):
    from .. import qrpage
    qrpage.show(url, show_in_terminal, "用微信扫码登录", "扫码确认后，就能在微信里和电脑上的 Claude Code / Codex 对话。", "微信")


def scan_login(show=show_qr, timeout=480):
    """返回 {account_id, token, base_url, user_id}；没扫成返回 None。user_id 是扫码的人，就是主人。"""
    qr = _get(BASE, "ilink/bot/get_bot_qrcode?bot_type=3")
    code, url = qr.get("qrcode"), qr.get("qrcode_img_content")
    if not code or not url:
        return None
    show(url)
    base, refreshed, said = BASE, 0, False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _get(base, f"ilink/bot/get_qrcode_status?qrcode={code}", timeout=POLL_TIMEOUT + 5)
        except (OSError, ValueError):
            time.sleep(1)
            continue
        status = r.get("status") or "wait"
        if status == "scaned" and not said:
            print("已扫码，请在微信里确认…", flush=True)
            said = True
        elif status == "scaned_but_redirect" and r.get("redirect_host"):
            base = f"https://{r['redirect_host']}"
        elif status == "expired":
            refreshed += 1
            if refreshed > 3:
                print("二维码过期好几次了。")
                return None
            qr = _get(BASE, "ilink/bot/get_bot_qrcode?bot_type=3")
            code, url = qr.get("qrcode"), qr.get("qrcode_img_content")
            print("二维码过期了，换了一张新的：")
            show(url)
        elif status == "confirmed":
            if r.get("ilink_bot_id") and r.get("bot_token"):
                return {"account_id": r["ilink_bot_id"], "token": r["bot_token"],
                        "base_url": r.get("baseurl") or BASE, "user_id": r.get("ilink_user_id") or ""}
            return None
        time.sleep(1)
    print("等扫码超时了。")
    return None


# ---------- 加解密、排版 ----------

def _encrypt(data, key):
    pad = 16 - len(data) % 16
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data + bytes([pad]) * pad) + enc.finalize()


def _decrypt(data, key):
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1] if out else 0
    return out[:-pad] if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad) else out


def _aes_key(b64):
    """收到的 aes_key 有两种写法：base64(16 字节) 或 base64(32 位十六进制字符串)。"""
    raw = base64.b64decode(b64)
    if len(raw) == 16:
        return raw
    return bytes.fromhex(raw.decode("ascii"))


def plain(md):
    """微信不显示 Markdown：去掉加粗、标题井号、代码块围栏，链接写成「文字（网址）」。代码块里的内容原样保留。"""
    out, code = [], False
    for line in (md or "").split("\n"):
        if re.match(r"^\s*```", line):
            code = not code
            continue
        if code:
            out.append(line)
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: m.group(2) if m.group(1) == m.group(2)
                      else f"{m.group(1)}（{m.group(2)}）", line)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------- 渠道实现 ----------

class Weixin(Channel):
    name = "weixin"
    label = "微信"
    cards = False
    style = ("微信不显示 Markdown，用纯文字和换行组织内容：不要用标题、加粗、表格、代码块围栏，"
             "列表用「1.」「-」开头，链接直接写网址。")

    def __init__(self, project_path, entry, secret, handlers):
        super().__init__(project_path, entry, secret, handlers)
        self.base = (entry.get("base_url") or BASE).rstrip("/")
        self.http = None
        self.poller = None
        self.ok = False
        self.seen = {}
        self.media = {}                       # 收到的图片、文件：key → 消息里的媒体信息，download 时用
        self.tickets = {}                     # 用户 → (typing_ticket, 取到的时间)
        self.lock = asyncio.Lock()            # 一条一条发，拆开的长消息不会乱序
        self.store = os.path.join(registry.pdir(project_path), "weixin.json")
        self.saved = self._load()             # {buf: 收消息的游标, ctx: {用户: context_token}}

    def _load(self):
        try:
            with open(self.store, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if d.get("account") != self.entry["app_id"]:      # 重新扫码换了机器人：旧游标、旧 token 都作废
            d = {}
        return {"account": self.entry["app_id"], "buf": d.get("buf", ""), "ctx": d.get("ctx") or {}}

    def _save(self):
        tmp = self.store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.saved, f, ensure_ascii=False)
        os.replace(tmp, self.store)

    # ----- 接口 -----

    async def _post(self, endpoint, payload, timeout=API_TIMEOUT):
        body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}}, ensure_ascii=False,
                          separators=(",", ":")).encode()
        uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode()).decode()
        headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": uin,
                   "iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION,
                   "Authorization": f"Bearer {self.secret}"}
        r = await self.http.post(f"{self.base}/{endpoint}", content=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _code(r):
        """返回码：ret 和 errcode 哪个不是 0 就是哪个。"""
        for k in ("ret", "errcode"):
            if r.get(k) not in (0, None):
                return r[k]
        return 0

    # ----- 收消息 -----

    async def start(self):
        self.http = httpx.AsyncClient(timeout=API_TIMEOUT)
        # 先用一个马上返回的接口验一下登录（长轮询没消息时要挂 35 秒）
        owner = self.entry.get("owner_open_id") or ""
        payload = {"ilink_user_id": owner, **({"context_token": self.saved["ctx"][owner]} if owner in self.saved["ctx"] else {})}
        r = await self._post("ilink/bot/getconfig", payload, timeout=10)
        if self._code(r) == EXPIRED:
            raise RuntimeError(EXPIRED_TIP)
        self.ok = True
        self.poller = asyncio.create_task(self._poll())

    async def stop(self):
        if self.poller:
            self.poller.cancel()
        if self.http:
            await self.http.aclose()

    def connected(self):
        return self.ok

    async def _poll(self):
        timeout, fails = POLL_TIMEOUT, 0
        while True:
            try:
                r = await self._post("ilink/bot/getupdates", {"get_updates_buf": self.saved["buf"]}, timeout=timeout + 10)
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                continue
            except Exception as e:
                fails += 1
                self.ok = fails < 3 and self.ok
                log.warning("微信收消息出错（%s），%d 秒后重试", str(e)[:120] or type(e).__name__, 2 if fails < 3 else 30)
                await asyncio.sleep(2 if fails < 3 else 30)
                continue
            code = self._code(r)
            if code == EXPIRED:
                if not self.error:
                    log.error(EXPIRED_TIP)
                self.ok, self.error = False, EXPIRED_TIP
                await asyncio.sleep(60)               # 用户重新扫码后会换新 token、重启进程；在那之前隔一会儿再看看
                continue
            if code:
                fails += 1
                log.warning("微信收消息出错：%s %s", code, r.get("errmsg") or "")
                await asyncio.sleep(2 if fails < 3 else 30)
                continue
            fails, self.ok, self.error = 0, True, None
            if isinstance(r.get("longpolling_timeout_ms"), int) and r["longpolling_timeout_ms"] > 0:
                timeout = r["longpolling_timeout_ms"] / 1000
            await self._handle(r)

    async def _handle(self, r):
        for m in r.get("msgs") or []:
            try:
                await self._on_message(m)
            except Exception:
                log.exception("处理微信消息出错")
        buf = r.get("get_updates_buf")
        if buf and buf != self.saved["buf"]:              # 先处理再存游标：中途退出的话这批下次还会收到
            self.saved["buf"] = buf
            self._save()

    def _dup(self, mid):
        now = time.time()
        if mid in self.seen:
            return True
        self.seen[mid] = now
        if len(self.seen) > 2000:
            self.seen = {k: v for k, v in self.seen.items() if now - v < 3600}
        return False

    async def _on_message(self, m):
        if m.get("message_type") != MSG_USER:                # 机器人自己发的也会收到
            return
        sender = str(m.get("from_user_id") or "")
        mid = str(m.get("message_id") or m.get("seq") or uuid.uuid4().hex)
        if not sender or self._dup(mid):
            return
        if m.get("group_id"):
            log.info("忽略群消息（微信只支持私聊）")
            return
        if m.get("context_token") and self.saved["ctx"].get(sender) != m["context_token"]:
            self.saved["ctx"][sender] = m["context_token"]
            self._save()
        texts, files, kind = [], [], "text"
        for i, it in enumerate(m.get("item_list") or []):
            t = it.get("type")
            if t == ITEM_TEXT:
                text = (it.get("text_item") or {}).get("text") or ""
                quoted = self._quote(it.get("ref_msg") or {})
                texts.append(f"（引用：{quoted}）\n{text}" if quoted else text)
            elif t == ITEM_VOICE:
                said = (it.get("voice_item") or {}).get("text")
                if said:
                    texts.append(said)
                else:
                    kind = "voice"
            elif t in (ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO):
                key = f"{mid}-{i}"
                self.media[key] = it
                name = (it.get("file_item") or {}).get("file_name") if t == ITEM_FILE else None
                files.append(("image" if t == ITEM_IMAGE else "file", key, name))
                kind = "image" if t == ITEM_IMAGE else "file"
            else:
                kind = f"item{t}"
        if len(self.media) > 200:
            self.media = dict(list(self.media.items())[-100:])
        inc = Incoming(chat_id=sender, chat_type="p2p", sender_id=sender, message_id=mid,
                       text="\n".join(x for x in texts if x).strip(), files=files, kind=kind)
        await self.handlers["on_message"](inc)

    @staticmethod
    def _quote(ref):
        it = ref.get("message_item") or {}
        if it.get("type") == ITEM_TEXT:
            s = (it.get("text_item") or {}).get("text") or ""
        elif it:
            s = {ITEM_IMAGE: "[图片]", ITEM_FILE: "[文件]", ITEM_VIDEO: "[视频]", ITEM_VOICE: "[语音]"}.get(it.get("type"), "")
        else:
            s = ref.get("title") or ""
        s = " ".join(s.split())
        return s if len(s) <= 200 else s[:200] + "…"

    # ----- 发消息 -----

    async def _send_items(self, to, items, client_id):
        """发一条消息。网络出错、限流重试；对方的 context_token 失效了就不带它再发一次。"""
        ctx = self.saved["ctx"].get(to)
        waits = list(RETRY_WAITS)
        while True:
            msg = {"from_user_id": "", "to_user_id": to, "client_id": client_id, "message_type": MSG_BOT,
                   "message_state": 2, "item_list": items}
            if ctx:
                msg["context_token"] = ctx
            try:
                r = await self._post("ilink/bot/sendmessage", {"msg": msg})
                code = self._code(r)
                err = f"{code} {r.get('errmsg') or ''}".strip()
            except httpx.HTTPError as e:
                code, err = None, str(e)[:120] or type(e).__name__
            if code == 0:
                return
            if code == EXPIRED and ctx:
                ctx = None
                continue
            if code == EXPIRED:
                self.ok, self.error = False, EXPIRED_TIP
                raise RuntimeError(EXPIRED_TIP)
            if code not in (None, RATE_LIMIT) or not waits:
                raise RuntimeError(f"微信接口出错：{err}")
            wait = waits.pop(0) * (3 if code == RATE_LIMIT else 1)
            log.warning("微信发消息出错（%s），%d 秒后重试", err, wait)
            await asyncio.sleep(wait)

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        uid = (uid or uuid.uuid4().hex)[:32]
        parts = split_text(plain(markdown) or "（空）", TEXT_LIMIT)
        async with self.lock:
            for i, part in enumerate(parts):
                if i:
                    await asyncio.sleep(1)    # 连着发太快会被限流
                await self._send_items(chat_id, [{"type": ITEM_TEXT, "text_item": {"text": part}}], f"ccim-{uid}-{i}")

    @staticmethod
    def _render(card):
        lines = [f"【{card['title']}】"] if card.get("title") else []
        if card.get("body"):
            lines.append(plain(card["body"]))
        if card.get("buttons"):
            b = card["buttons"]
            lines.append(f"回复 y {b[0]['text']}，回复 n {b[-1]['text']}")
        elif card.get("note"):
            lines.append(card["note"])
        return "\n\n".join(lines)

    async def send_card(self, chat_id, card, reply_to=None):
        """微信消息发出去就改不了，卡片当成一条文字发。"""
        await self.send_text(chat_id, self._render(card))
        return None

    async def update_card(self, message_id, card):
        pass

    async def delete_message(self, message_id):
        pass

    async def send_to_user(self, user_id, markdown):
        await self.send_text(user_id, markdown)
        return user_id

    async def typing(self, chat_id, on):
        try:
            ticket, at = self.tickets.get(chat_id, (None, 0))
            if not ticket or time.time() - at > TICKET_TTL:
                payload = {"ilink_user_id": chat_id}
                if self.saved["ctx"].get(chat_id):
                    payload["context_token"] = self.saved["ctx"][chat_id]
                ticket = (await self._post("ilink/bot/getconfig", payload, timeout=10)).get("typing_ticket")
                if not ticket:
                    return
                self.tickets[chat_id] = (ticket, time.time())
            await self._post("ilink/bot/sendtyping", {"ilink_user_id": chat_id, "typing_ticket": ticket,
                                                      "status": 1 if on else 2}, timeout=10)
        except Exception as e:
            log.debug("「正在输入」没发出去：%s", e)

    # ----- 文件 -----

    async def send_file(self, chat_id, path):
        name = os.path.basename(path)
        size = os.path.getsize(path)
        if size > FILE_LIMIT:
            raise RuntimeError(f"{name} 有 {size // 1024 // 1024} MB，太大了，微信发不了")
        with open(path, "rb") as f:
            data = f.read()
        mime = mimetypes.guess_type(name)[0] or ""
        media_type = MEDIA_IMAGE if mime.startswith("image/") else MEDIA_VIDEO if mime.startswith("video/") else MEDIA_FILE
        filekey, key = secrets.token_hex(16), secrets.token_bytes(16)
        md5 = hashlib.md5(data).hexdigest()
        cipher = _encrypt(data, key)
        up = await self._post("ilink/bot/getuploadurl", {
            "filekey": filekey, "media_type": media_type, "to_user_id": chat_id, "rawsize": len(data), "rawfilemd5": md5,
            "filesize": len(cipher), "no_need_thumb": True, "aeskey": key.hex()})
        if self._code(up):
            raise RuntimeError(f"微信接口出错：{self._code(up)} {up.get('errmsg') or ''}".strip())
        url = up.get("upload_full_url") or (up.get("upload_param") and
                                            f"{CDN}/upload?encrypted_query_param={quote(up['upload_param'], safe='')}"
                                            f"&filekey={quote(filekey, safe='')}")
        if not url:
            raise RuntimeError("微信没有给上传地址")
        r = await self.http.post(url, content=cipher, headers={"Content-Type": "application/octet-stream"},
                                 timeout=FILE_TIMEOUT)
        param = r.headers.get("x-encrypted-param") if r.status_code == 200 else None
        if not param:
            raise RuntimeError(f"上传到微信失败：HTTP {r.status_code}")
        # aes_key 要写成 base64(十六进制字符串)，写成 base64(原始字节) 图片会显示成灰块
        media = {"encrypt_query_param": param, "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1}
        if media_type == MEDIA_IMAGE:
            item = {"type": ITEM_IMAGE, "image_item": {"media": media, "mid_size": len(cipher)}}
        elif media_type == MEDIA_VIDEO:
            item = {"type": ITEM_VIDEO, "video_item": {"media": media, "video_size": len(cipher), "play_length": 0,
                                                       "video_md5": md5}}
        else:
            item = {"type": ITEM_FILE, "file_item": {"media": media, "file_name": name, "len": str(len(data))}}
        async with self.lock:
            await self._send_items(chat_id, [item], f"ccim-{uuid.uuid4().hex[:32]}")

    async def download(self, message_id, kind, key, name, folder):
        it = self.media.pop(key, None)
        if not it:
            raise RuntimeError("找不到这个文件了，请重新发一次")
        t = it.get("type")
        payload = it.get({ITEM_IMAGE: "image_item", ITEM_FILE: "file_item", ITEM_VIDEO: "video_item"}[t]) or {}
        media = payload.get("media") or {}
        if media.get("encrypt_query_param"):
            url = f"{CDN}/download?encrypted_query_param={quote(media['encrypt_query_param'], safe='')}"
        elif (media.get("full_url") or "").startswith("https://") and ".weixin.qq.com/" in media["full_url"]:
            url = media["full_url"]
        else:
            raise RuntimeError("微信没有给下载地址")
        r = await self.http.get(url, timeout=FILE_TIMEOUT)
        r.raise_for_status()
        data = r.content
        aes = payload.get("aeskey")                           # 图片可能直接带十六进制的 aeskey
        key = bytes.fromhex(aes) if t == ITEM_IMAGE and aes else _aes_key(media["aes_key"]) if media.get("aes_key") else None
        if key:
            data = _decrypt(data, key)
        ext = {ITEM_IMAGE: ".jpg", ITEM_VIDEO: ".mp4"}.get(t, "")
        name = re.sub(r"[/\\\x00]", "_", name or payload.get("file_name") or f"{key_name(message_id)}{ext}")
        path = inbox_path(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


def key_name(message_id):
    return re.sub(r"[^0-9A-Za-z]", "", str(message_id))[-12:] or "file"
