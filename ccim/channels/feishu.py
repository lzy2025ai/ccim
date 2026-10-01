"""飞书渠道。

扫码建机器人：飞书的设备码注册接口（init → begin → poll），做法参考 NousResearch/hermes-agent 的飞书接入；
这个接口不在飞书公开文档里，所以保留手动填 App ID / App Secret 的退路。
收消息、按钮回调：lark-oapi 的长连接客户端，不需要公网地址。它跑在自己的线程和事件循环里，
收到的事件转交到 ccim 主循环处理。
"""
import asyncio, io, json, logging, os, re, secrets, threading, time, uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody, CreateImageRequest, CreateImageRequestBody,
                                 CreateMessageReactionRequest, CreateMessageReactionRequestBody, CreateMessageRequest,
                                 CreateMessageRequestBody, DeleteMessageReactionRequest, DeleteMessageRequest,
                                 GetChatRequest, GetMessageRequest, GetMessageResourceRequest,
                                 PatchMessageRequest, PatchMessageRequestBody, ReplyMessageRequest,
                                 ReplyMessageRequestBody)
from lark_oapi.event.callback.model.p2_card_action_trigger import (CallBackCard, CallBackToast,
                                                                    P2CardActionTriggerResponse)

from .base import CardAction, Channel, Incoming, parse_address

log = logging.getLogger("ccim.feishu")

ACCOUNTS = {"feishu": "https://accounts.feishu.cn", "lark": "https://accounts.larksuite.com"}
OPEN = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
REG_PATH = "/oauth/v1/app/registration"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
FILE_TYPES = {".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls",
              ".ppt": "ppt", ".pptx": "ppt", ".opus": "opus"}
TEXT_LIMIT = 3800          # 单条消息的字数上限，超过就拆成几条
FILE_LIMIT = 30 * 1024 * 1024
IMAGE_LIMIT = 10 * 1024 * 1024
DEFAULT_REACTION = "OnIt"  # 收到消息时加在消息上的表情，表示收到、正在处理
API_TIMEOUT = 10           # 飞书接口偶尔会卡住不回，等太久不如重试
FILE_TIMEOUT = 120
RETRY_WAITS = (1, 3)       # 网络出错、限流、服务端出错时重试，两次之间等几秒
RETRY_CODES = {99991400, 99991663, 99991672, 1000004, 1000005}   # 限流、服务端繁忙


# ---------- 扫码配对 ----------

def _post(domain, body):
    req = Request(ACCOUNTS[domain] + REG_PATH, data=urlencode(body).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:                    # 轮询时「还没扫码」是用 400 返回的，正文照样是 JSON
        raw = e.read()
        if raw:
            return json.loads(raw.decode())
        raise


def _http_json(url, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def probe(app_id, secret, domain):
    """用凭证换 token 并读机器人信息，顺便验证凭证。返回 (机器人名, 机器人 open_id)。"""
    t = _http_json(OPEN[domain] + "/open-apis/auth/v3/tenant_access_token/internal",
                   {"app_id": app_id, "app_secret": secret})
    token = t.get("tenant_access_token")
    if not token:
        raise SystemExit(f"App ID 或 App Secret 不对：{t.get('msg')}")
    b = _http_json(OPEN[domain] + "/open-apis/bot/v3/info", token=token)
    bot = b.get("bot") or (b.get("data") or {}).get("bot") or {}
    return bot.get("app_name") or bot.get("bot_name") or "", bot.get("open_id") or ""


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
    print("\n用飞书手机端扫码，确认后会在你的飞书账号下建好一个机器人：\n")
    _print_qr(url)
    print(f"\n扫不了的话，在手机飞书里打开这个链接：\n{url}\n")


def scan_register(timeout=600, show=show_in_terminal):
    """返回 {app_id, secret, domain, owner_open_id}；扫码失败返回 None。show(url) 负责把二维码给用户看。"""
    domain = "feishu"
    init = _post(domain, {"action": "init"})
    if "client_secret" not in (init.get("supported_auth_methods") or []):
        return None
    b = _post(domain, {"action": "begin", "archetype": "PersonalAgent", "auth_method": "client_secret",
                       "request_user_info": "open_id"})
    code, url = b.get("device_code"), b.get("verification_uri_complete")
    if not code or not url:
        return None
    show(url)
    interval = b.get("interval") or 5
    deadline = time.monotonic() + min(b.get("expires_in") or b.get("expire_in") or 600, timeout)
    while time.monotonic() < deadline:
        try:
            r = _post(domain, {"action": "poll", "device_code": code, "tp": "ob_app"})
        except (URLError, OSError, ValueError):
            time.sleep(interval)
            continue
        user = r.get("user_info") or {}
        if user.get("tenant_brand") == "lark":
            domain = "lark"
        if r.get("client_id") and r.get("client_secret"):
            return {"app_id": r["client_id"], "secret": r["client_secret"], "domain": domain,
                    "owner_open_id": user.get("open_id") or ""}
        if r.get("error") in ("access_denied", "expired_token"):
            print("扫码被取消或二维码过期了。")
            return None
        time.sleep(interval)
    print("等扫码超时了。")
    return None


def _manual():
    print("\n改为手动填写。在飞书开放平台建一个企业自建应用，开启机器人能力，事件和回调都选「长连接」，\n"
          "订阅「接收消息」「机器人进群」事件和「卡片回传交互」回调，然后把凭证填在这里。\n")
    app_id = input("App ID：").strip()
    secret = input("App Secret：").strip()
    domain = "lark" if input("是 Lark 国际版吗？(y/N)：").strip().lower() == "y" else "feishu"
    return {"app_id": app_id, "secret": secret, "domain": domain, "owner_open_id": ""}


# ---------- 渠道实现 ----------

class Feishu(Channel):
    name = "feishu"

    @staticmethod
    def pair(project_path, show=show_in_terminal, interactive=True):
        """interactive=False（handoff 里配对）时不问问题，扫码失败直接报错。"""
        try:
            got = scan_register(show=show)
        except (URLError, OSError, ValueError) as e:
            print(f"连不上飞书的扫码服务：{e}")
            got = None
        if not got and not interactive:
            raise SystemExit("没有配对成功。可以在终端里进到项目目录运行 ccim，改用手动填写 App ID 和 App Secret。")
        if not got:
            if input("改用手动填写 App ID 和 App Secret 吗？(y/N)：").strip().lower() != "y":
                raise SystemExit("没有配对。")
            got = _manual()
        bot_name, bot_open_id = probe(got["app_id"], got["secret"], got["domain"])
        entry = {"channel": "feishu", "app_id": got["app_id"], "domain": got["domain"], "bot_name": bot_name,
                 "bot_open_id": bot_open_id, "owner_open_id": got["owner_open_id"], "group": "owner",
                 "secret": got["secret"]}
        if not entry["owner_open_id"]:
            # 手动填写拿不到扫码人，用一次性口令认主人：谁在私聊里发了这串字，谁就是主人
            entry["claim_code"] = secrets.token_hex(3)
        return entry

    def __init__(self, project_path, entry, secret, handlers):
        super().__init__(project_path, entry, secret, handlers)
        self.domain = lark.LARK_DOMAIN if entry.get("domain") == "lark" else lark.FEISHU_DOMAIN
        build = lambda timeout: (lark.Client.builder().app_id(entry["app_id"]).app_secret(secret).domain(self.domain)
                                 .timeout(timeout).log_level(lark.LogLevel.WARNING).build())
        self.api = build(API_TIMEOUT)
        self.api_files = build(FILE_TIMEOUT)     # 上传、下载文件慢，单独放宽
        self.loop = None
        self.ws = None
        self.thread_error = None
        self.seen = {}                        # 飞书可能重复推送同一条消息，按消息 id 去重

    # ----- 长连接 -----

    async def start(self):
        self.loop = asyncio.get_running_loop()
        builder = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message)
                   .register_p2_card_action_trigger(self._on_action)
                   .register_p2_im_chat_member_bot_added_v1(self._on_bot_added))
        # 飞书还会推来表情增删（机器人自己加的「敲键盘」也算）、撤回、已读、进出群等事件；
        # 没注册处理函数的事件 SDK 会报 processor not found，飞书还会反复重推，所以都接下来不处理
        for name in ("message_message_read_v1", "message_reaction_created_v1", "message_reaction_deleted_v1",
                     "message_recalled_v1", "chat_access_event_bot_p2p_chat_entered_v1", "chat_member_bot_deleted_v1",
                     "chat_member_user_added_v1", "chat_member_user_deleted_v1", "chat_member_user_withdrawn_v1",
                     "chat_updated_v1", "chat_disbanded_v1"):
            builder = getattr(builder, f"register_p2_im_{name}")(lambda data: None)
        handler = builder.build()

        self.handler = handler
        self.ws = lark.ws.Client(self.entry["app_id"], self.secret, event_handler=handler, domain=self.domain,
                                 log_level=lark.LogLevel.WARNING)
        threading.Thread(target=self._run_ws, name="feishu-ws", daemon=True).start()
        for _ in range(60):                   # 等长连接建好（最多 15 秒）
            await asyncio.sleep(0.25)
            if self.thread_error:
                raise RuntimeError(f"连不上飞书：{self.thread_error}")
            if getattr(self.ws, "_conn", None) is not None:
                return
        raise RuntimeError("连接飞书超时")

    def _run_ws(self):
        """长连接线程。lark 自己会断线重连；万一整个客户端退出了，启动成功过的就等 10 秒重建再连。"""
        import lark_oapi.ws.client as wsmod
        ever_connected = False
        while True:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            wsmod.loop = loop                 # lark 的长连接客户端用的是模块级事件循环，换成这个线程自己的
            try:
                self.ws.start()
            except Exception as e:
                if not ever_connected and self.ws._conn is None:
                    self.thread_error = str(e) or type(e).__name__
                    log.error("飞书长连接退出：%s", e)
                    return
                log.warning("飞书长连接断了（%s），10 秒后重连", short_err(e))
            finally:
                ever_connected = ever_connected or self.ws._conn is not None
                try:
                    loop.close()
                except Exception:
                    pass
            time.sleep(10)
            self.ws = lark.ws.Client(self.entry["app_id"], self.secret, event_handler=self.handler, domain=self.domain,
                                     log_level=lark.LogLevel.WARNING)

    def connected(self):
        return getattr(self.ws, "_conn", None) is not None

    def _submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: f.exception() and log.error("处理事件出错", exc_info=f.exception()))
        return fut

    # ----- 收事件（都在长连接线程里被调用） -----

    def _dup(self, mid):
        now = time.time()
        if mid in self.seen:
            return True
        self.seen[mid] = now
        if len(self.seen) > 2000:
            self.seen = {k: v for k, v in self.seen.items() if now - v < 3600}
        return False

    def _on_message(self, data):
        ev = data.event
        msg, sender = ev.message, ev.sender
        if not msg or self._dup(msg.message_id):
            return
        if getattr(sender, "sender_type", "") != "user":
            return
        try:
            content = json.loads(msg.content or "{}")
        except ValueError:
            content = {}
        bot_id = self.entry.get("bot_open_id")
        mentioned = False
        for m in msg.mentions or []:
            if bot_id and getattr(m.id, "open_id", None) == bot_id:
                mentioned = True
        if not bot_id and msg.mentions:       # 没拿到机器人自己的 id 时退一步：群里机器人只收得到 @ 它的消息
            mentioned = True
        text, files = self._parse(msg.message_type, content, msg.mentions or [], bot_id)
        inc = Incoming(chat_id=msg.chat_id, chat_type=msg.chat_type, sender_id=sender.sender_id.open_id,
                       message_id=msg.message_id, text=text, mentioned_bot=mentioned,
                       files=files, kind=msg.message_type, thread_id=msg.thread_id, root_id=msg.root_id)
        self._submit(self.handlers["on_message"](inc))

    @staticmethod
    def _parse(mtype, content, mentions, bot_id=None):
        """返回 (文字, [(类型, key, 文件名)])。图片、文件之后由 download 下载。@机器人 的占位符去掉，@别人 换成名字。"""
        names = {m.key: "" if bot_id and getattr(m.id, "open_id", None) == bot_id else f"@{m.name}" for m in mentions}

        def clean(s):
            for k, n in names.items():
                s = s.replace(k, n)
            return " ".join(s.split(" ")).strip()

        files = []
        if mtype == "text":
            return clean(content.get("text", "")), files
        if mtype == "post":
            post = content if "content" in content else next(iter(content.values()), {}) if content else {}
            parts = [post.get("title") or ""]
            for line in post.get("content") or []:
                seg = []
                for el in line:
                    tag = el.get("tag")
                    if tag in ("text", "md"):
                        seg.append(el.get("text", ""))
                    elif tag == "a":
                        seg.append(f"{el.get('text', '')}({el.get('href', '')})")
                    elif tag == "at":
                        seg.append("" if el.get("user_id") in (None, "") else f"@{el.get('user_name', '')}")
                    elif tag == "img" and el.get("image_key"):
                        files.append(("image", el["image_key"], None))
                    elif tag == "code_block":
                        seg.append(f"\n```\n{el.get('text', '')}\n```\n")
                parts.append("".join(seg))
            return clean("\n".join(p for p in parts if p)), files
        if mtype == "image":
            files.append(("image", content.get("image_key"), None))
            return "", files
        if mtype in ("file", "media", "audio"):
            files.append(("file", content.get("file_key"), content.get("file_name")))
            return "", files
        return "", files

    def _on_action(self, data):
        ev = data.event
        ctx = ev.context
        act = CardAction(chat_id=getattr(ctx, "open_chat_id", "") or "", message_id=getattr(ctx, "open_message_id", ""),
                         operator_id=getattr(ev.operator, "open_id", "") or "", value=ev.action.value or {})
        try:
            card, toast = self._submit(self.handlers["on_action"](act)).result(timeout=2.5)
        except Exception:
            log.exception("处理按钮回调出错")
            card, toast = None, None
        resp = P2CardActionTriggerResponse()
        if toast:
            resp.toast = CallBackToast()
            resp.toast.type, resp.toast.content = "info", toast
        if card:
            resp.card = CallBackCard()
            resp.card.type, resp.card.data = "raw", self._render(card)
        return resp

    def _on_bot_added(self, data):
        self._submit(self.handlers["on_bot_added"](data.event.chat_id))

    # ----- 发消息 -----

    async def _call(self, fn, req, retries=RETRY_WAITS):
        """调飞书接口。网络出错、超时、限流会重试；发消息的请求带着固定的 uuid，重试不会发出两条。"""
        for attempt in range(len(retries) + 1):
            try:
                r = await asyncio.to_thread(fn, req)
            except Exception as e:                # requests 的连接错误、超时
                err, retry = f"{type(e).__name__}: {short_err(e)}", True
            else:
                if r.success():
                    return r
                err, retry = f"{r.code} {r.msg}", r.code in RETRY_CODES or (r.code or 0) >= 500 and (r.code or 0) < 600
            if not retry or attempt == len(retries):
                raise RuntimeError(f"飞书接口出错：{err}")
            log.warning("飞书接口出错（%s），%d 秒后重试", err, retries[attempt])
            await asyncio.sleep(retries[attempt])

    async def _send(self, addr, msg_type, content, reply_to=None, uid=None):
        """addr 带话题时：有 reply_to 就回复进话题，没有就直接发到话题里。
        uid：飞书按它去重（一小时内），同一条消息重发时传同一个 uid，就不会收到两条。"""
        chat_id, thread_id = parse_address(addr)
        content = json.dumps(content, ensure_ascii=False)
        uid = uid or uuid.uuid4().hex
        if reply_to:
            req = (ReplyMessageRequest.builder().message_id(reply_to)
                   .request_body(ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
                                 .reply_in_thread(bool(thread_id)).uuid(uid).build()).build())
            r = await self._call(self.api.im.v1.message.reply, req)
        else:
            req = (CreateMessageRequest.builder().receive_id_type("thread_id" if thread_id else "chat_id")
                   .request_body(CreateMessageRequestBody.builder().receive_id(thread_id or chat_id).msg_type(msg_type)
                                 .content(content).uuid(uid).build()).build())
            r = await self._call(self.api.im.v1.message.create, req)
        return r.data.message_id

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        uid = uid or uuid.uuid4().hex
        for i, part in enumerate(split_text(markdown or "（空）")):
            post = {"zh_cn": {"content": [[{"tag": "md", "text": part}]]}}
            await self._send(chat_id, "post", post, reply_to if i == 0 else None, uid=f"{uid[:40]}-{i}")

    @staticmethod
    def _render(card):
        colors = {"blue": "blue", "green": "green", "orange": "orange", "red": "red", "grey": "grey"}
        els = [{"tag": "markdown", "content": card.get("body") or " "}]
        if card.get("buttons"):
            els.append({"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": b["text"]},
                 "type": b.get("style", "default"), "value": b.get("value", {})} for b in card["buttons"]]})
        if card.get("note"):
            els.append({"tag": "note", "elements": [{"tag": "plain_text", "content": card["note"]}]})
        return {"config": {"wide_screen_mode": True, "update_multi": True},
                "header": {"title": {"tag": "plain_text", "content": card.get("title", "")},
                           "template": colors.get(card.get("color"), "blue")},
                "elements": els}

    async def send_card(self, chat_id, card, reply_to=None):
        return await self._send(chat_id, "interactive", self._render(card), reply_to)

    async def update_card(self, message_id, card):
        req = (PatchMessageRequest.builder().message_id(message_id)
               .request_body(PatchMessageRequestBody.builder()
                             .content(json.dumps(self._render(card), ensure_ascii=False)).build()).build())
        await self._call(self.api.im.v1.message.patch, req, retries=(1,))   # 卡片下次刷新还会再更新，少重试

    async def send_to_user(self, user_id, markdown):
        post = {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
        req = (CreateMessageRequest.builder().receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder().receive_id(user_id).msg_type("post")
                             .content(json.dumps(post, ensure_ascii=False)).uuid(uuid.uuid4().hex).build()).build())
        r = await self._call(self.api.im.v1.message.create, req)
        return r.data.chat_id

    async def chat_name(self, chat_id):
        try:
            r = await self._call(self.api.im.v1.chat.get, GetChatRequest.builder().chat_id(chat_id).build())
            return r.data.name or ""
        except Exception:
            return ""

    async def get_text(self, message_id):
        try:
            r = await self._call(self.api.im.v1.message.get, GetMessageRequest.builder().message_id(message_id).build())
            m = r.data.items[0]
            text, files = self._parse(m.msg_type, json.loads(m.body.content or "{}"), m.mentions or [])
            return text or ("[图片]" if files and files[0][0] == "image" else "[文件]" if files else "")
        except Exception:
            log.debug("读取消息失败", exc_info=True)
            return ""

    async def delete_message(self, message_id):
        await self._call(self.api.im.v1.message.delete, DeleteMessageRequest.builder().message_id(message_id).build())

    async def add_reaction(self, message_id, emoji=None):
        """默认用「OnIt」（在办了），可以用 ccim config reaction=表情名 换；表情名无效时退回「OK」。"""
        for e in dict.fromkeys([emoji or self.entry.get("reaction") or DEFAULT_REACTION, "OK"]):
            try:
                req = (CreateMessageReactionRequest.builder().message_id(message_id)
                       .request_body(CreateMessageReactionRequestBody.builder().reaction_type({"emoji_type": e}).build())
                       .build())
                r = await self._call(self.api.im.v1.message_reaction.create, req, retries=(1,))
                return message_id, r.data.reaction_id
            except Exception as err:
                log.warning("加表情 %s 失败（%s）", e, err)
        return None

    async def remove_reaction(self, message_id, reaction_id):
        req = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        await self._call(self.api.im.v1.message_reaction.delete, req)

    # ----- 文件 -----

    async def send_file(self, chat_id, path):
        size = os.path.getsize(path)
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        with open(path, "rb") as f:
            buf = io.BytesIO(f.read())
        buf.name = name
        if ext in IMAGE_EXT and size <= IMAGE_LIMIT:
            req = (CreateImageRequest.builder()
                   .request_body(CreateImageRequestBody.builder().image_type("message").image(buf).build()).build())
            r = await self._call(_rewind(buf, self.api_files.im.v1.image.create), req)
            return await self._send(chat_id, "image", {"image_key": r.data.image_key})
        if size > FILE_LIMIT:
            raise RuntimeError(f"{name} 有 {size // 1024 // 1024} MB，超过飞书 30 MB 的上限")
        req = (CreateFileRequest.builder()
               .request_body(CreateFileRequestBody.builder().file_type(FILE_TYPES.get(ext, "stream"))
                             .file_name(name).file(buf).build()).build())
        r = await self._call(_rewind(buf, self.api_files.im.v1.file.create), req)
        return await self._send(chat_id, "media" if ext == ".mp4" else "file", {"file_key": r.data.file_key})

    async def download(self, message_id, kind, key, name, folder):
        req = (GetMessageResourceRequest.builder().message_id(message_id).file_key(key)
               .type("image" if kind == "image" else "file").build())
        r = await self._call(self.api_files.im.v1.message_resource.get, req)
        name = name or r.file_name or (key + (".jpg" if kind == "image" else ""))
        name = re.sub(r"[/\\\x00]", "_", name)
        path = os.path.join(folder, f"{time.strftime('%m%d-%H%M%S')}-{name}")
        with open(path, "wb") as f:
            f.write(r.file.read())
        return path


def _rewind(buf, fn):
    """上传接口会把文件读到底，重试前要拨回开头。"""
    def call(req):
        buf.seek(0)
        return fn(req)
    return call


def short_err(e):
    s = str(e)
    m = re.search(r"(Read timed out|Max retries exceeded|Connection (?:aborted|refused|reset)|Name or service not known|"
                  r"nodename nor servname provided|timed out)", s)
    return m.group(1) if m else s[:120]


def split_text(s, limit=TEXT_LIMIT):
    """按段落拆长消息，尽量不把代码块拆开。"""
    if len(s) <= limit:
        return [s]
    out, cur = [], ""
    for para in s.split("\n"):
        while len(para) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    # 被拆断的代码块：前一段补上结尾、后一段补上开头
    fixed, open_fence = [], False
    for part in out:
        if open_fence:
            part = "```\n" + part
        open_fence = part.count("```") % 2 == 1
        fixed.append(part + ("\n```" if open_fence else ""))
    return fixed
