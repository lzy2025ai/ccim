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
import asyncio, glob, json, logging, os, queue, re, shutil, subprocess, threading, time, uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

from . import agents, registry
from .peers import Server
from . import devices, docs, filesend, perf, share, tokens, workspace
from . import apps as apps_mod
from . import kb as kb_mod
from . import skills as skills_mod
from . import board as board_mod
from . import diary as diary_mod
from .schedules import is_quiet as schedules_quiet, listed
from .channels.base import CardAction, Channel, Incoming, chatlog_text, inbox_path

log = logging.getLogger("ccim.platform")

PORT = 8977
CHAT_PAGE = 50                                # 整体状态里带最近多少条对话，更早的每页多少条（#268）
OWNER = "web"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
STATIC = {"/manifest.webmanifest": "application/manifest+json", "/apple-touch-icon.png": "image/png",
          "/icon-192.png": "image/png", "/icon-512.png": "image/png"}    # 添加到主屏幕用（#17）
STATIC.update({f"/avatars/{name}.webp": "image/webp" for name in agents.AVATARS})


def static_file(path):
    """内置图标、头像和 manifest；只提供白名单里的随包资源。"""
    if path not in STATIC:
        return None
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", path.lstrip("/")), "rb") as f:
        return f.read(), STATIC[path]
UPLOAD_LIMIT = 100 * 1024 * 1024
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".svg")
MARKS = ("title", "pin", "star", "archived", "archived_at", "icon", "color", "hidden", "expanded")   # 对话列表里用户加的标记（star：星标，#194；expanded：挂件对话点过放大，#326；archived：已完成，#369 以前叫归档）
ICONS = ["star", "flag", "bolt", "bug", "heart", "bookmark"]
COLORS = ["red", "orange", "yellow", "green", "blue", "purple"]
# 页面专用的助手（#116）
GUEST_HELPER = ("\n- 这个用户用的是别人共享给他的电脑：你装、改、删的是他自己的那一份 Skill，只有他自己的 Agent 能用，不会同步到别的电脑。"
                "skill_list 里标了 shared 的是电脑主人的，只能看、能用，不能改也不能删。只能从 GitHub 地址安装。")
DRAFT_PROMPT = """下面是用户和 AI 助手的一段对话。用户想把这个助手保存成一个长期使用的 Agent，以后专门干这类活。
请给它起个名字，再写一段角色说明（以后每次和它对话都会带上这段话）：
- 名字：不超过 8 个字，说清楚它干什么，比如「前端开发」「周报助手」
- 角色说明：它负责什么；从对话里看出来的用户要求、习惯、偏好、约定（具体写，比如用什么技术、交付什么格式、哪些事不能做）；
  不写这次对话的具体进度和一次性的事。用「你」称呼它，3 到 8 句，不用标题和列表符号

对话：
{talk}

按这个格式输出，不要别的：
名字：……
角色说明：……"""

HELPERS = {"skills": {"title": "Skill 助手", "role": (
    "你是 ccim 平台的 Skill 助手，在 Skill 页面右侧和用户对话，帮他管理「平台 Skill」：装在平台上、同步到所有电脑、"
    "所有 Agent 在 ccim 里对话时都能用的 Skill。\n"
    "- 查看、修改、新建、安装、停用、删除平台 Skill 一律用 skill_ 开头的工具，不要直接改 ~/.ccim/skills 下的文件"
    "（直接改不会同步到别的电脑）。\n"
    "- 改文件前先用 skill_read 读原文，改完用一两句话说改了什么。新建 Skill 先写 SKILL.md（开头 --- 里要有 name 和 description）。\n"
    "- 找 Skill：可以查 https://skills.sh/api/search?q=关键词（返回安装量）、https://clawhub.ai/api/v1/search?q=关键词、"
    "https://skillsmp.com/api/skills?search=关键词，也可以上网搜；找到 GitHub 地址后用 skill_install 装，装之前说清楚装的是什么。\n"
    "- 删除前工具会弹出确认，用户点了才会删。\n"
    "- 用户消息开头括号里是他在页面上正看着的 Skill 和文件，「这个」一般就指它。\n"
    "- 回答用中文，简短。")}}
# 侧边对话（#196）：主对话干活时，在侧栏里另开一个对话问问题，不打断它
SIDE_ROLE = ("\n\n## 侧边对话\n"
             "你在「侧边对话」里：用户的主对话可能正在干活，用户不想打断它，来这里问问题。你是从主对话分出来的，知道之前聊过的内容；"
             "用户消息前面括号里是平台自动附上的（不是用户贴的）：主对话在那之后的新进展、正在进行的那一轮做到哪了。\n"
             "- 你只能看、不能动：可以读文件、搜索、跑只看不改的命令（git status、git log、查日志等）；"
             "不删改文件、不装东西、不提交推送、不启动或停止进程，用户明确要求也不做，现在不做、以后也不做。\n"
             "- 命令在这里报没权限、没登录、读不到配置时，可能是侧边对话的限制，先别下结论说工具坏了、登录过期了；"
             "照实说在这里跑不了，请用户回主对话做，或 @ 一个 Agent 去做。\n"
             "- 用户要你动手时，说明这里只能问问题，请他回主对话说，可以把要对主对话说的话写好给他。\n"
             "- 想请别的 Agent 来看看、或让它去动手，用户在这里的输入框打 @ 选它就行（它也只能看，要动手会新开它自己的对话去做）。\n"
             "- 回答简短。")
# 侧边对话里被 @ 的 Agent（#290）：和侧边对话一样只读，要动手就新开自己的对话去做
SIDE_AT_ROLE = ("\n\n## 在侧边对话里被 @\n"
                "用户在某个对话旁边开了侧边对话，在侧边对话里 @ 了你，请你看看、出出主意。消息前面括号里是平台附上的主对话和侧边对话的内容，"
                "主对话里的人看不到你在这里说的话。\n"
                "- 你在这里只能看、不能动：可以读文件、搜索、跑只看不改的命令；不删改文件、不装东西、不提交推送、不启动或停止进程。\n")
SIDE_AT_WORK = ("- 需要动手的事（改代码、提 PR、跑长任务），或者用户让你去做（比如「去改吧」）：先在回答里用一句话说要新开对话去做，"
                "再调用 start_work 新开一个你自己的对话去做。text 要写清楚来龙去脉、要做什么、做完怎么算好，新对话看不到这里。"
                "调用后这一轮简短收尾；侧边栏里有卡片显示进度，那边做完后结果会作为一条新消息交给你，由你在这里向用户汇报。\n"
                "- 只看不改的事，命令在这里被拦下了（报没权限、没登录、读不到配置）：这是侧边对话的限制，不是那个工具真出了问题。"
                "不要只回一句做不了，也照上面的办法新开对话去做。\n- 回答简短。")
SIDE_AT_FAR = ("- 需要动手的事（改代码、提 PR、跑长任务）：在这里做不了，请用户直接和你开一个对话去做，可以把要交代的话写好给用户。\n"
               "- 回答简短。")
SIDE_CTX = 6000                                                    # 每次带上的主对话新进展，最多这么多字
MERGED = ("card", "reply", "run", "mention", "secret", "edit", "work")           # 记录里会被更新的：同一个 id 后来的覆盖前面的（edit：工作区连着自动保存，#206）


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
        self.tag = None                       # 平台：本机的推送标上属于哪个访客（#154）
        self.on_publish = []                  # 每条推送先给这些看一眼（整体状态的缓存据此作废，#258）

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def alive(self, q):
        with self.lock:
            return q in self.subs

    def publish(self, ev):
        for fn in self.on_publish:
            fn(ev)
        if self.tag and not ev.get("_m"):
            ev = self.tag(ev)
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:                 # 这个页面卡住太久：断开它，浏览器会重连再整份拉一次
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
                    if r.get("type") in MERGED and r.get("id") and r["id"] in pos:
                        old = out[pos[r["id"]]]
                        # 卡片每次都是整张：处理过的卡片没有按钮，合并的话旧按钮会留下，看着还在等批准（#48）
                        out[pos[r["id"]]] = {**(r if r["type"] == "card" else {**old, **r}), "at": old["at"]}
                        continue
                    if r.get("type") in MERGED and r.get("id"):
                        pos[r["id"]] = len(out)
                    out.append(r)
        except OSError:
            pass
        return out

    def truncate(self, chat, n):
        """只留前 n 条（按合并后的算）。"""
        self._rewrite(chat, self.read(chat)[:n])

    def drop(self, chat, rid):
        """去掉某一条（按 id）。"""
        self._rewrite(chat, [r for r in self.read(chat) if r.get("id") != rid])

    def _rewrite(self, chat, rows):
        with self.lock:
            tmp = self._f(chat) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, self._f(chat))

    def rename(self, chat, new):
        with self.lock:
            for a, b in ((self._f(chat), self._f(new)), (docs.snap_dir(self.folder, chat), docs.snap_dir(self.folder, new))):
                if os.path.exists(a):
                    os.replace(a, b)

    def remove(self, chat):
        try:
            os.remove(self._f(chat))
        except OSError:
            pass
        docs.remove_snaps(self.folder, chat)


# ---------- 网页渠道 ----------

class WebChannel(Channel):
    name = "web"
    label = "网页"
    cards = False
    live = True
    style = "可以用 Markdown（标题、加粗、列表、表格、代码块、链接）。"
    files_note = ("把这台电脑上的文件、文件夹交给用户时，默认写完整路径、用反引号包起来（不写 file:// 链接），"
                  "网页上点一下就能打开、下载、在访达中显示。图片、视频要让用户在聊天里直接看，文件不在项目目录里，"
                  "或者用户要你直接发文件时，调用 ccim 的 send_file 工具发到聊天里。")

    def __init__(self, project_path, entry, secret, handlers):
        super().__init__(project_path, entry, secret, handlers)
        self.events = Events()
        self.logs = Logs(os.path.join(registry.pdir(project_path), "web"))
        self.progress_of = {}                 # 对话 → 正在干活的进度
        self.bg_of = {}                       # 对话 → 一轮结束后还在跑的后台任务（#271）
        self.done_steps = {}                  # 对话 → 刚做完那一轮的步骤，接下来的回答带上它
        self.card_chat = {}                   # 卡片 id → 对话
        self.secrets = {}                     # 等用户输密码的卡片 id → (对话, future)
        self.turn_t0 = {}                     # 对话 → 这一轮开始的时刻（找 AI 这一轮改了哪些文件，#206）
        self.user_saved = {}                  # (对话, 文件) → 用户在网页里保存后的修改时间，不算 AI 改的
        self.server = None

    async def start(self):
        self.platform = self.handlers["platform"]
        self.server = self.platform.serve()

    async def stop(self):
        if self.server:
            self.server.shutdown()

    def _emit(self, chat, rec):
        # chat 是「哪个对话的推送」；记录自己也可能有 chat（@ 回答指向它的子对话），另放一个键，网页收到后还原
        ev = dict(rec, chat=chat)
        if rec.get("chat") and rec["chat"] != chat:
            ev["rec_chat"] = rec["chat"]
        self.events.publish(ev)

    def context(self, chat_id, ctx):
        """答完取到了上下文占用：开着这个对话的网页更新进度圈（#183）。"""
        self._emit(chat_id, {"type": "ctx", "ctx": ctx})

    def idle(self, chat_id):
        """这一轮结束的状态已经记下：叫网页再取一次列表。"""
        self.events.publish({"type": "idle", "chat": chat_id})

    def background(self, chat_id, tasks, moved=False):
        if tasks:
            self.bg_of[chat_id] = tasks
        elif not self.bg_of.pop(chat_id, None):
            return
        self._emit(chat_id, {"type": "bg", "tasks": tasks, "moved": moved})

    async def progress(self, chat_id, snap):
        if snap["state"] == "working":
            if chat_id not in self.progress_of:
                self.turn_t0[chat_id] = time.time() - (snap.get("elapsed") or 0)
            self.progress_of[chat_id] = snap
        else:
            self.progress_of.pop(chat_id, None)
            self.done_steps[chat_id] = dict(snap, done_at=time.monotonic())
        self._emit(chat_id, {"type": "progress", **snap})

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        done = self.done_steps.pop(chat_id, None)
        if done and schedules_quiet(markdown):     # 定时任务这次没什么要说的（#122）：不记、不提醒
            return
        if done and time.monotonic() - done["done_at"] > 10:   # 那一轮没有回答：这是之后的提示，不带步骤
            done = None
        if done:
            rec = {"type": "answer", "text": markdown or "", "steps": done["steps"], "state": done["state"],
                   "elapsed": done["elapsed"]}
            if len(done.get("said") or []) > 1 or done.get("said") and done["said"][0][0] < len(done["steps"]):
                rec["said"] = done["said"]       # 中间说过的话（#203）；只有最后那句回答就不用记
            if done.get("fork"):
                rec["fork"] = done["fork"]
            if done.get("prompt"):
                rec["prompt"] = done["prompt"]
            changed = await self._changed(chat_id, done)
            if changed:
                rec["changed"] = changed
        else:
            rec = {"type": "note", "text": markdown or ""}
        self._emit(chat_id, self.logs.append(chat_id, rec))
        if rec["type"] == "answer" and getattr(self, "on_answer", None):
            self.on_answer(chat_id)

    async def _changed(self, chat_id, done):
        """这一轮 AI 改了工作目录里的哪些文件（#206）：[{path, new}]。"""
        t0 = self.turn_t0.pop(chat_id, None)
        if t0 is None:
            return []
        try:
            root = self.platform.chat_dir(chat_id)
        except Exception:
            return []
        if not os.path.isdir(root):
            return []
        mine = {p: m for (c, p), m in self.user_saved.items() if c == chat_id}

        def skip(p, mtime):
            return p in mine and abs(mine[p] - mtime) < 0.001

        try:
            rows = await asyncio.to_thread(workspace.changed_since, root, t0, done.get("changed") or [], skip, done.get("created") or [])
        except Exception:
            log.warning("找这一轮改了哪些文件时出错", exc_info=True)
            return []
        for k in [k for k in self.user_saved if k[0] == chat_id]:
            self.user_saved.pop(k, None)
        rroot = os.path.realpath(root)
        return [r for r in rows if r["path"] == rroot or r["path"].startswith(rroot + os.sep)]

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
        sub, chat_id = chat_id, self.outer(chat_id)         # 被 @ 的 Agent 发的：记到群里（#357）
        rec = {"type": "file", "path": os.path.realpath(path), "name": os.path.basename(path), "size": os.path.getsize(path)}
        if sub != chat_id:
            rec["sub"] = sub                  # 记下是哪个被 @ 的 Agent 发的，网页里跟着它的回答排（#380）
        snap = docs.snapshot(self.logs.folder, chat_id, path)      # 文档存一份，之后能切换看各版本
        if snap:
            rec["snap"] = snap
        elif os.path.splitext(path)[1].lower() in IMAGES and rec["size"] <= 30 << 20:
            # 图片也存一份：Agent 常常反复写同一个文件名（截图），不存的话之前那几条消息也会显示成最新的（#56）
            try:
                d = docs.snap_dir(self.logs.folder, chat_id)
                os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, f"{uuid.uuid4().hex[:10]}-{os.path.basename(path)}")
                shutil.copy(path, dest)
                rec.update(path=dest, orig=rec["path"])
            except OSError:
                pass
        self._emit(chat_id, self.logs.append(chat_id, rec))

    def outer(self, chat_id):
        """被 @ 的 Agent 的子对话 → 用户看着的那个对话（在这台电脑上的话），不是子对话就是它自己（#337、#345）。"""
        chats = self.platform.chats()
        of = (chats.get(chat_id) or {}).get("mention_of")
        return of if of in chats else chat_id

    async def send_app(self, chat_id, app):
        """Agent 用 app_open 打开应用（#103）：记一条，网页在对话右侧打开它，之后点这条还能再打开。
        被 @ 的 Agent 在子对话里干活，记到用户正看着的那个对话里（#337）。那个对话在别的电脑上的照旧记在子对话里。"""
        sub, chat_id = chat_id, self.outer(chat_id)
        rec = {"type": "app", "app": app["id"], "name": app["name"], "icon": app.get("icon") or ""}
        if sub != chat_id:
            rec["sub"] = sub
        self._emit(chat_id, self.logs.append(chat_id, rec))

    async def send_chatlog(self, chat_id, log):
        """聊天记录（#218）：网页里显示成卡片。图片存一份，原图之后被删、被覆盖也还看得到。"""
        sub, chat_id = chat_id, self.outer(chat_id)
        items = []
        for it in log["items"]:
            it = dict(it)
            if it.get("image"):
                try:
                    d = docs.snap_dir(self.logs.folder, chat_id)
                    os.makedirs(d, exist_ok=True)
                    dest = os.path.join(d, f"{uuid.uuid4().hex[:10]}-{os.path.basename(it['image'])}")
                    shutil.copy(it["image"], dest)
                    it["image"] = dest
                except OSError:
                    pass
            items.append(it)
        rec = {"type": "chatlog", "title": log["title"], "items": items, "text": chatlog_text(log)}
        if sub != chat_id:
            rec["sub"] = sub
        self._emit(chat_id, self.logs.append(chat_id, rec))

    async def download(self, message_id, kind, key, name, folder):
        """网页上传的文件先放在平台的 uploads 里，这里挪到项目的收件箱。"""
        safe = re.sub(r"[/\\\\]", "_", name or "file")      # f-string 里不能有反斜杠（Python 3.11）
        dest = inbox_path(folder, safe)
        shutil.copy(key, dest)
        return dest

    async def ask_secret(self, chat_id, command, reason, timeout):
        """管理员权限：弹卡片让用户看命令、输开机密码。返回密码，不同意或超时返回 None。密码不记下来。"""
        sid = "sec-" + uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.secrets[sid] = (chat_id, fut)
        self._emit(chat_id, self.logs.append(chat_id, {"type": "secret", "id": sid, "command": command, "reason": reason,
                                                       "state": "pending"}))
        state, pw = "expired", None
        try:
            pw = await asyncio.wait_for(fut, timeout)
            from .admin import DIALOG
            state = "dialog" if pw is DIALOG else "done" if pw is not None else "declined"
            return pw
        except asyncio.TimeoutError:
            return None
        finally:
            self.secrets.pop(sid, None)
            self._emit(chat_id, self.logs.append(chat_id, {"type": "secret", "id": sid, "state": state}))
            pw = None

    def answer_secret(self, sid, password):
        """HTTP 线程里调：把密码（或 None = 不执行）交给等着的那一轮。"""
        hit = self.secrets.get(sid)
        if not hit or hit[1].done():
            return False
        fut = hit[1]
        self.platform.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(password))
        return True

    async def send_to_user(self, user_id, markdown):
        return None


# ---------- 对话调度 ----------

class Store:
    """网页对话的记录都在平台的 state.json 的 chats 里。"""

    def __init__(self, home):
        self.home = home
        self.deleted = set()                  # 删掉的对话：Chat 断开时还会写一次状态，不能让它冒回来
        self.ver = 0                          # 写过几次：share.owner_map 据此重算

    def read(self):
        return registry.read_state(self.home).get("chats") or {}

    def write(self, chats):
        for cid in self.deleted:
            chats.pop(cid, None)
        registry.write_state(self.home, chats=chats)
        self.ver += 1


class Problem(Exception):
    """给用户看的错误。"""


class Platform:
    def __init__(self, home, entry):
        self.home, self.entry = home, entry
        self.channel = None
        self.loop = None
        self.store = Store(home)
        self.bridges = {}                     # Agent 编号或 temp:<对话> → Bridge
        self.peer_results = {}                # 别的电脑派来的活：编号 → 结果（还没做完是 None）
        self.peer_runs = {}                   # 编号 → (子对话, 开始时刻)：问结果时带上这期间打开的应用（#337）
        self.background = set()
        self.works = set()                    # 侧边对话里派出去、还在做的（work 卡片的 id，#290）
        from .projects import Projects
        from .schedules import Schedules
        from .mentions import Mentions
        self.projects = Projects(self)
        self.schedules = Schedules(self)
        self.mentions = Mentions(self)
        skills_mod.ON_CHANGE.append(lambda: (self.kb_changed(), self.channel.events.publish({"type": "skills", "chat": None})))
        self.cleaner = devices.Cleaner(registry.pdir(home))
        from . import devpanel                # 开发页（#156）：只在跑开发版的电脑上有
        self.dev = devpanel.Dev(self) if devpanel.enabled() else None
        self.titles = devpanel.Titles(self)  # 任务面板上对话在做的 issue 的标题（#157）
        from .peers import Shared
        self.shared = Shared()                # 同时到的 /api/state 只算一次（#258）

    def handlers(self):
        return {"platform": self}

    def _dev_loop(self):
        """开发页的数据（要问 GitHub）在后台定时刷新，红点读缓存。"""
        from . import devpanel
        while True:
            before = self.dev.badge()
            self.dev.refresh()
            if self.dev.badge() != before:
                self.dev._publish()
            time.sleep(devpanel.REFRESH)

    def _dev(self):
        if not self.dev:
            raise Problem("这台电脑上没有开发版。")
        return self.dev

    def attach(self, channel):
        self.channel = channel
        channel.events.on_publish.append(self.shared.changed)
        self.loop = asyncio.get_event_loop()
        self.schedules.start()                # 到点就跑的定时任务
        from .peers import Peers
        self.peers = Peers(self)
        self.peers.start()
        try:                                  # 项目复制成群聊（只做一次）
            from . import groups
            groups.migrate_projects(self)
        except Exception:
            log.exception("项目复制成群聊出错")
        try:
            self._split_old_subs()
        except Exception:
            log.exception("子对话另开成独立对话出错")
        try:
            self._stars_to_pins()
        except Exception:
            log.exception("星标转成置顶出错")
        self.background.add(asyncio.ensure_future(self._models()))
        threading.Thread(target=self._warm, daemon=True).start()
        self.background.add(asyncio.ensure_future(self._cleanup_loop()))
        if self.dev:
            threading.Thread(target=self._dev_loop, daemon=True).start()
        channel.kbs_of = lambda cid: (self.chats().get(cid) or {}).get("kbs") or []   # 挂在对话上的知识库（#27）
        from . import board                   # 任务面板（#48）：每轮回答完更新一句话进度
        channel.on_answer = lambda cid: self.background.add(asyncio.ensure_future(board.refresh_brief(self, cid)))
        st = registry.read_state(self.home)
        if not st.get("board_seen_init"):    # 第一次：已有的对话都算看过
            chats = self.chats()
            for m in chats.values():
                m.setdefault("seen_at", time.time())
            self.store.write(chats)
            registry.write_state(self.home, board_seen_init=True)
        self.kb_wake = threading.Event()
        threading.Thread(target=self._kb_sync_loop, daemon=True).start()
        threading.Thread(target=self._diary_flush_loop, daemon=True).start()   # 暂存的日记补写（#376）
        tokens.start()                        # 任务面板的 Token 用量（#240），第一次要读一会儿，放后台
        channel.events.tag = lambda ev: share.tag(self, ev)

    # ----- 知识库（#27） -----

    def _kb_sync_loop(self):
        """问在线的其他电脑要知识库、平台 Skill（#101）的清单，比自己新的拉过来。每 30 秒一次，有改动时马上一次。"""
        from . import kb
        time.sleep(15)
        while True:
            for m in self.peers.machines():
                if m["me"] or not m.get("online"):
                    continue
                for mod, base, what, ev in ((kb, "/api/kb", "知识库", "kb"), (skills_mod, "/api/skills", "Skill", "skills")):
                    try:
                        st, _, data = self.peers.forward(m["id"], "GET", base + "/manifest", b"", {})
                        if st != 200:
                            continue
                        pull, drop, tombs = mod.wanted(json.loads(data))
                        contents = {}
                        if pull:
                            st, _, data = self.peers.forward(m["id"], "POST", base + "/export", json.dumps({"keys": pull}).encode(),
                                                             {"Content-Type": "application/json"})
                            contents = json.loads(data) if st == 200 else {}
                        if mod.apply(contents, drop, tombs):
                            log.info("%s从「%s」同步了 %d 项，删了 %d 项", what, m["name"], len(contents), len(drop))
                            self.channel.events.publish({"type": ev, "chat": None})
                    except Exception:
                        log.debug("%s同步出错（%s）", what, m.get("name"), exc_info=True)
            self.kb_wake.wait(30)
            self.kb_wake.clear()

    def kb_changed(self):
        """这台电脑上改了知识库：叫在线的其他电脑马上来拉。"""
        def go():
            for m in self.peers.machines():
                if not m["me"] and m.get("online"):
                    try:
                        self.peers.forward(m["id"], "POST", "/api/kb/sync_now", b"{}", {"Content-Type": "application/json"})
                    except Exception:
                        pass
        threading.Thread(target=go, daemon=True).start()

    def _issue(self, cid, m):
        if not board_mod.top_level(m, cid):
            return None
        try:
            from . import devpanel
            ats = board_mod.summary(self.channel.logs, cid).get("user_ats") or [0]
            return devpanel.issue_of(self, cid, m, since=ats[0])
        except Exception:
            log.debug("认 issue 出错", exc_info=True)
            return None

    def _board_marks(self, cid, m):
        if not board_mod.top_level(m, cid):
            return {"attention": None, "unread": False, "ask": ""}
        summ = board_mod.summary(self.channel.logs, cid)
        rounds = summ.get("rounds") or 0
        crew = self.mentions.crew(cid)         # 群里被 @ 的成员在干活：和主 Agent 在干活一样，先不算等你处理（#192）
        att = board_mod.attention(m, summ)
        return {"attention": None if crew and att != "ask" else att, "unread": board_mod.unread(m, summ),
                "ask": board_mod.ask_preview(summ.get("last_user")), "last_by": summ.get("last_by"), "crew": crew,
                "rounds": rounds, "answered": bool(summ.get("answer_at")) and not (m.get("schedule") and not rounds)}

    def dismiss(self, cid):
        """任务面板里「知道了」：不再等你处理，不算看过（#65）。"""
        chats = self.chats()
        if cid in chats:
            chats[cid]["dismissed_at"] = time.time()
            self.store.write(chats)
        return {}

    def mark_seen(self, cid):
        """网页里看过这个对话了（任务面板的未读，#48）。"""
        chats = self.chats()
        if cid in chats:
            chats[cid]["seen_at"] = time.time()
            self.store.write(chats)
        return {}

    def _agent_bridges(self, aid):
        """这个 Agent 的 Bridge：本身、项目目录里的（@）、访客的（#）、侧边对话的（|）。"""
        return [b for key, b in self.bridges.items() if key == aid or key.startswith(aid) and key[len(aid)] in "@#|"]

    def relaunch_agent(self, aid):
        """Agent 改了要换启动参数的设置（chrome、角色说明）：它已经开着的对话下一条消息时按原会话续接，带上新参数。"""
        for b in self._agent_bridges(aid):
            for c in b.chats.values():
                c.reconnect = True

    def agent_chats(self, aid, keys=None, channels=False):
        """改 Agent 的这些设置会影响到的对话（#387）：聊过、没完成、没单独设置这几项（keys 为空就不看这条）。
        channels：连它接的飞书、微信里聊过的一起数（只读，由它们的进程自己照办）。"""
        sid = "codex_session_id" if (agents.get(aid) or {}).get("base") == "codex" else "session_id"
        free = lambda m: not keys or any(not m.get(k) and k not in (m.get("keep") or {}) for k in keys)
        out = [cid for cid, m in self.chats().items()
               if m.get("agent") == aid and m.get(sid) and not m.get("archived") and not m.get("hidden") and free(m)]
        for kind, proc, path in agents.linked_paths(aid) if channels else ():
            chats = registry.read_state(proc).get("chats") or {}
            chats = (chats.get(path) or {}) if kind == "weixin" else chats
            out += [f"{kind}:{c}" for c, m in chats.items() if isinstance(m, dict) and m.get(sid) and free(m)]
        return out

    def keep_dir(self, aid, path):
        """Agent 换了工作目录（#387）：聊过的对话还在原来的目录接着聊（会话记录按目录存），新对话用新的。"""
        chats = self.chats()
        sid = "codex_session_id" if (agents.get(aid) or {}).get("base") == "codex" else "session_id"
        for m in chats.values():
            if m.get("agent") == aid and m.get(sid) and not m.get("cwd"):
                m["cwd"] = path
        self.store.write(chats)

    def apply_agent(self, aid, keys, before, live):
        """Agent 的模型、思考深度改了（#387）。live：已开的对话下一条消息起换上新的；否则它们留着改之前 Agent 的值。"""
        a = agents.get(aid) or {}
        if not live:
            chats = self.chats()
            for cid in self.agent_chats(aid, keys):
                m = chats[cid]
                m["keep"] = {**(m.get("keep") or {}), **{k: before.get(k) for k in keys if not m.get(k)}}
            self.store.write(chats)
        for b in self._agent_bridges(aid):
            b.entry.update({k: a.get(k) for k in keys})
            if live:
                for c in b.chats.values():
                    c.refollow(keys)

    # ----- 临时对话保存为 Agent（#318） -----

    def adoptable(self, cid):
        """能不能归到新 Agent 名下；能的话返回 (底座, 它现在的目录, 用户有没有选过目录)。"""
        m = self.chats().get(cid)
        if not m or not m.get("temp") or m.get("agent"):
            raise Problem("只有临时对话能保存为 Agent。")
        if m.get("helper") or m.get("side_of"):
            raise Problem("这个对话不能保存为 Agent。")
        if m.get("busy"):
            raise Problem("它还在干活，等这一轮做完再保存。")
        t = m["temp"]
        return t.get("base") or "claude", self.chat_dir(cid), bool(t.get("dir"))

    async def adopt_chat(self, cid, aid):
        """对话归到 Agent 名下：会话编号存在对话状态里，不动；目录和 Agent 的不一样就记在对话上，接着在原目录聊，会话才接得上。"""
        a = agents.get(aid)
        path = self.chat_dir(cid)
        b = self.bridges.pop("temp:" + cid, None)
        c = b and b.chats.pop(cid, None)
        if c:
            await c.stop()
            if c.worker:
                c.worker.cancel()
        chats = self.chats()
        m = chats[cid]
        m.pop("temp", None)
        m["agent"] = aid
        if os.path.realpath(path) != os.path.realpath(agents.cwd(a)):
            m["cwd"] = path
        self.store.write(chats)

    async def draft_agent(self, cid):
        """按对话内容起草 Agent 的名字和角色说明。"""
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
        from .session import CLI
        rows = [r for r in self.channel.logs.read(cid) if r.get("type") in ("user", "answer") and r.get("text")]
        lines = [("用户：" if r["type"] == "user" else "助手：") + r["text"].strip()[:1500] for r in rows]
        talk = "\n\n".join(lines)
        if len(talk) > 12000:
            talk = talk[:4000] + "\n\n……\n\n" + talk[-8000:]
        if not talk:
            return {"name": "", "role": ""}
        opts = ClaudeAgentOptions(model="sonnet", max_turns=1, allowed_tools=[], setting_sources=[],
                                  cwd=os.path.expanduser("~"), cli_path=CLI, permission_mode="default",
                                  extra_args={"no-session-persistence": None})
        text = ""
        async for msg in query(prompt=DRAFT_PROMPT.format(talk=talk), options=opts):
            if isinstance(msg, AssistantMessage):
                text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        name, _, role = text.strip().partition("\n")
        name = name.partition("：")[2] if name.startswith("名字") else name
        role = role.strip()
        role = role.partition("：")[2].strip() if role.startswith("角色说明") else role
        return {"name": " ".join(name.split()).strip("「」\"'“”")[:20], "role": role[:2000]}

    def set_kbs(self, cid, ids):
        from . import kb
        ids = [x for x in (ids or []) if kb.dirs([x])][:8]
        chats = self.chats()
        if cid not in chats:
            raise Problem("找不到这个对话。")
        chats[cid]["kbs"] = ids
        self.store.write(chats)
        try:
            self._bridge(cid).chat(cid, "p2p").reconnect = True     # 下一条消息开始带上
        except Problem:
            pass
        return {"kbs": ids}

    # ----- 已完成的对话到期清理（#26；#369 以前叫归档） -----

    async def _cleanup_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self.cleanup_archived()
            except Exception:
                log.exception("清理已完成的对话出错")
            await asyncio.sleep(3600)

    async def cleanup_archived(self):
        now = time.time()
        chats = self.chats()                  # 以前标的没记时间：从现在算起
        if any(m.get("archived") and not m.get("archived_at") for m in chats.values()):
            for m in chats.values():
                if m.get("archived") and not m.get("archived_at"):
                    m["archived_at"] = now
            self.store.write(chats)
        marks = registry.read_state(self.home).get("marks") or {}
        if any(mk.get("archived") and not mk.get("archived_at") for mk in marks.values()):
            for mk in marks.values():
                if mk.get("archived") and not mk.get("archived_at"):
                    mk["archived_at"] = now
            registry.write_state(self.home, marks=marks)
        self._done_backfill()                 # 先把完成记录记全，清理掉的对话统计和复盘里还在（#369）
        days = archive_days()
        if not days:
            return
        for cid, m in list(self.chats().items()):
            if m.get("archived") and not m.get("busy") and (delete_at(m, days) or now + 1) <= now:
                log.info("已完成的对话到期，清理：%s", m.get("title") or cid)
                await self.delete_chat(cid)
        due = [cid for cid, mk in marks.items() if mk.get("archived") and not mk.get("hidden")]
        if due:
            rows = {r["id"]: r for r in self.external_chats(terminal=True)}
            changed = False
            for cid in due:
                at = delete_at({**marks[cid], "last_active": (rows.get(cid) or {}).get("last_active")}, days)
                if at and at <= now:
                    marks[cid]["hidden"] = True
                    changed = True
            if changed:
                registry.write_state(self.home, marks=marks)

    # ----- 完成记录（#369）：标为已完成时记一条，取消就删；对话被清理掉后还留着 -----

    def _done_write(self, recs):
        import tempfile
        path = board_mod.done_file(self)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".done.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def _done_read(self):
        try:
            with open(board_mod.done_file(self), encoding="utf-8") as f:
                recs = json.load(f)
            return recs if isinstance(recs, dict) else {}
        except (OSError, ValueError):
            return {}

    def _done_entry(self, cid, at, row=None):
        """完成记录里的一条：完成时间、当时的标题、Agent、来源；访客的对话记下是谁的（对话清理掉后还要按人分）。"""
        if cid.startswith("x:"):
            if row is None:
                try:
                    row = self._external(cid)
                except Problem:
                    row = {}
            mk = (registry.read_state(self.home).get("marks") or {}).get(cid) or {}
            return {"at": at, "title": mk.get("title") or row.get("title") or "别处的对话", "agent": row.get("agent"),
                    "source": row.get("source")}
        v = self.chat_view(cid)
        return {"at": at, "title": v.get("title") or "新对话", "agent": v.get("agent"), "source": v.get("source"),
                "account": share.chat_owner(self, cid)}

    def _done_mark(self, cid, on, at=None):
        recs = self._done_read()
        if on:
            try:
                recs[cid] = self._done_entry(cid, at or time.time())
            except Problem:
                return
        elif recs.pop(cid, None) is None:
            return
        self._done_write(recs)

    def _done_backfill(self):
        """以前就标了（那时叫归档）、还没进完成记录的，按标的时间补上。"""
        recs = self._done_read()
        chats = self.chats()
        marks = registry.read_state(self.home).get("marks") or {}
        miss = [cid for cid, m in chats.items() if m.get("archived") and m.get("archived_at") and cid not in recs]
        xmiss = [cid for cid, mk in marks.items() if mk.get("archived") and mk.get("archived_at") and cid not in recs]
        if not miss and not xmiss:
            return
        for cid in miss:
            recs[cid] = self._done_entry(cid, chats[cid]["archived_at"])
        if xmiss:
            try:
                rows = {r["id"]: r for r in self.external_chats(terminal=True)}
            except Exception:
                rows = {}
            for cid in xmiss:
                recs[cid] = self._done_entry(cid, marks[cid]["archived_at"], rows.get(cid) or {})
        self._done_write(recs)

    def _stars_to_pins(self):
        """置顶和星标合成一个（#347）：以前只加了星标的转成置顶，排在原来置顶的后面。"""
        chats = self.chats()
        low = max([m["pin"] for m in chats.values() if m.get("pin") is not None] + [0])
        moved = 0
        for m in chats.values():
            if m.pop("star", None) is not None:
                moved += 1
                if m.get("pin") is None and not m.get("archived"):
                    low += 1
                    m["pin"] = low
        if moved:
            self.store.write(chats)
        marks = registry.read_state(self.home).get("marks") or {}
        if any("star" in m for m in marks.values()):
            for m in marks.values():
                if m.pop("star", None) and m.get("pin") is None and not m.get("archived"):
                    low += 1
                    m["pin"] = low
            registry.write_state(self.home, marks=marks)

    def _warm(self):
        """先把终端对话读一遍（认分叉要读对话记录，第一次慢），打开 Agent 页时就快了。"""
        try:
            self.external_chats(terminal=True)
        except Exception:
            log.exception("读终端对话出错")

    async def _models(self):
        """能选的模型列表：启动时、之后每天问一次 Claude Code。"""
        from . import models
        while True:
            if models.stale():
                await models.refresh()
            await asyncio.sleep(3600)

    # ----- 对话 -----

    def chats(self):
        return self.store.read()

    def meta(self, cid):
        c = self.chats().get(cid)
        if not c:
            raise Problem("找不到这个对话。")
        return c

    def chat_dir(self, cid):
        """这个对话在哪个目录里干活（工作区的根，#206）。和 _bridge 里一样算，只是不建目录、不开会话。"""
        m = self.meta(cid)
        if m.get("agent"):
            a = agents.get(m["agent"])
            if not a:
                raise Problem("这个对话的 Agent 已经删掉了。")
            return m.get("cwd") or agents.cwd(a)
        t = m.get("temp") or {}
        return t.get("dir") or os.path.join(registry.HOME, "scratch", t.get("scratch") or cid)

    def _ws_root(self, cid):
        root = self.chat_dir(cid)
        if share.chat_owner(self, cid) and os.path.realpath(root) in (workspace.HOME, "/"):
            raise Problem("这个对话的工作目录不能在这里打开。")     # 访客的对话：工作目录是整个主目录时不给看
        os.makedirs(root, exist_ok=True)
        return root

    def ws_call(self, cid, fn, *args):
        try:
            return fn(self._ws_root(cid), *args)
        except workspace.Problem as e:
            raise Problem(str(e))
        except PermissionError:
            raise Problem("没有权限。")
        except FileExistsError:
            raise Problem("已经有同名的了。")
        except OSError as e:
            raise Problem(f"没做成：{e.strerror or e}")

    def ws_in(self, cid, path):
        """这个文件在不在对话的工作目录里（工作区里打开、预览、下载用）。"""
        if not cid or cid.startswith("x:") or cid == devices.FS_CHAT or cid not in self.chats():
            return False
        try:
            workspace.resolve(self._ws_root(cid), path)
            return True
        except (workspace.Problem, Problem, OSError):
            return False

    def _bridge(self, cid):
        from .bridge import Bridge
        m = self.meta(cid)
        viewer = share.chat_owner(self, cid)     # 共享电脑上访客的对话（#154）
        if m.get("agent"):
            a = agents.get(m["agent"])
            if not a:
                raise Problem("这个对话的 Agent 已经删掉了。")
            path = m.get("cwd") or agents.cwd(a)         # 项目里的对话在项目目录（对比时在它自己的副本）里干活
            key = a["id"] if path == agents.cwd(a) else f"{a['id']}@{path}"
            lent = bool(viewer and viewer != a.get("account"))   # 主人共享给访客的 Agent：访客的对话单独一个 Bridge
            if lent:
                key += "#" + viewer
            if m.get("side_of"):              # 侧边对话（#196）：只读，单独一个 Bridge
                key += "|side"
            elif m.get("side_at"):            # 侧边对话里被 @ 的（#290）：也只读
                key += "|sideat" + ("-far" if m.get("remote_project") else "")
            os.makedirs(path, exist_ok=True)
            entry = {"agent": None if a.get("base") == "claude" else a.get("base"), "model": a.get("model"),
                     "effort": a.get("effort"), "role": a.get("role"), "chrome": a.get("chrome") and not viewer,
                     "agent_id": a["id"], "guest": viewer, "lent": lent}
            name = a["name"]
        else:
            t = m.get("temp") or {}
            key = "temp:" + cid
            path = t.get("dir") or os.path.join(registry.HOME, "scratch", t.get("scratch") or cid)
            os.makedirs(path, exist_ok=True)
            entry = {"agent": None if t.get("base", "claude") == "claude" else t["base"], "model": t.get("model"),
                     "effort": t.get("effort"), "guest": viewer}
            name = "临时对话"
            if m.get("helper") in HELPERS:        # 页面专用的助手（#116）：带上它的角色和工具
                h = HELPERS[m["helper"]]
                entry.update(helper=m["helper"], role=h["role"] + (GUEST_HELPER if viewer else ""))
                name = h["title"]
        if m.get("side_of"):
            entry.update(side=True, role=(entry.get("role") or "") + SIDE_ROLE)
        elif m.get("side_at"):
            entry.update(side=True, side_at=True, role=(entry.get("role") or "") + SIDE_AT_ROLE +
                         (SIDE_AT_FAR if m.get("remote_project") else SIDE_AT_WORK))
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

    def new_chat(self, agent=None, temp=None, fork_from=None, title=None, source=None, parent=None, fork_at=None, link=None,
                 account=None):
        cid = "w-" + uuid.uuid4().hex[:10]
        rec = {"type": "p2p", "created": registry.now(), "last_active": registry.now()}
        account = account or (self.chats().get(parent) or {}).get("account") if parent else account
        if account:                           # 共享电脑上访客开的对话（#154）
            rec["account"] = account
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
        if link:                              # 接着终端、桌面端的对话聊：就用原来的会话，不分支（#94）
            rec.update(origin=link, source=source, session_id=link, continued_at=time.time())
        if title:
            rec["title"] = title
        if parent:                            # 从网页里另一个对话的某条回答分出来
            rec.update(parent=parent, fork_from=fork_from, fork_at=fork_at)
            rec.pop("origin", None)
            rec.pop("source", None)
        chats = self.chats()
        chats[cid] = rec
        self.store.write(chats)
        return cid

    def split_sub(self, cid, force=False):
        """群成员的子对话（#150）：用户在里面自己开口（发消息、@ 人、拉人）就另开这个 Agent 的独立对话，
        接着子对话的会话（分支），之前的记录带过去；子对话只留群里 @ 它的内容。子对话跟着外层群聊（不进列表、
        一起删），单独聊出来的东西不能挂在它下面。不是子对话就原样返回。在主循环里调。"""
        from .mentions import Mentions
        m = self.meta(cid)
        if not m.get("mention_of"):
            return cid
        if m.get("busy") and not force:
            raise Problem("它正在回答群里的问题，等它答完再接着聊。")
        if not agents.get(m.get("agent")):
            raise Problem("这个对话的 Agent 已经删掉了。")
        sid = m.get("codex_session_id" if self._effective(m)["base"] == "codex" else "session_id")
        of = m["mention_of"]
        new = self.new_chat(agent=m["agent"])
        chats = self.chats()
        rec, old = chats[new], chats[cid]
        if sid:
            rec["fork_from"] = sid
        if m.get("title") and m["title"] != (chats.get(of) or {}).get("title"):   # 子对话的标题默认就是群聊的
            rec["title"] = m["title"]
        rec["split_from"] = of
        for k in ("cwd", "kbs", "members", "note", "note_at", "note_seen", "mention_seen"):
            if m.get(k):
                rec[k] = m[k]
        if isinstance(m.get("ctx_upto"), dict):   # 子对话自己当过群聊（老版本）：各人看到哪
            rec["ctx_upto"] = dict(m["ctx_upto"])
            old.pop("ctx_upto")
        for k in ("members", "note", "note_at", "note_seen", "mention_seen"):
            old.pop(k, None)
        moved = {}                            # 它里面 @ 过的人的子对话（本机的）搬到新对话下面
        for c, x in list(chats.items()):
            if x.get("mention_of") == cid:
                moved[c] = Mentions.sub(new, x["agent"])
                chats[moved[c]] = dict(x, mention_of=new)
                del chats[c]
                self.store.deleted.add(c)
        logs = self.channel.logs
        rows = logs.read(cid)
        for r in rows:
            if r.get("type") == "mention" and r.get("machine") and r.get("agent") in rec.get("ctx_upto", {}):
                rec["ctx_upto"].pop(r["agent"])       # 别的电脑上的子对话搬不过去，下次带上全部内容
        self.store.write(chats)
        for c, to in moved.items():
            logs.rename(c, to)
        for r in rows:
            if r.get("type") in ("user", "answer", "file", "chatlog", "note", "mention"):
                logs.append(new, dict(r, chat=moved[r["chat"]]) if r.get("chat") in moved else r)
        if any(r.get("type") == "mention" for r in rows):
            logs._rewrite(cid, [r for r in rows if r.get("type") != "mention"])
        log.info("子对话 %s 另开成独立对话 %s", cid, new)
        return new

    def _split_old_subs(self):
        """升级前用户在子对话里说过话、拉过人的，另开成独立对话（只做一次）。"""
        st = registry.read_state(self.home)
        if st.get("subs_split"):
            return
        for cid, m in list(self.chats().items()):
            if not m.get("mention_of") or not agents.get(m.get("agent")):
                continue
            mine = isinstance(m.get("ctx_upto"), dict) or [a for a in m.get("members") or [] if a != m.get("agent")]
            if not mine and not m.get("remote_project"):   # 群里 @ 它时记的那条带 from_chat；别的电脑派来的老记录没带，只看成员
                mine = any(r.get("type") == "user" and not r.get("from_chat") for r in self.channel.logs.read(cid))
            if mine:
                self.split_sub(cid, force=True)
        registry.write_state(self.home, subs_split=True)

    def _refs(self, cid, ids):
        """用户在输入框里引用的这个 Agent 以前的对话（#199）：只认同一个 Agent 的，返回 [{id, title}]。"""
        aid = self.meta(cid).get("agent")
        owners = share.owner_map(self)
        acct = owners.get(cid)                # 共享电脑上的访客（#154）：只认它自己账号的
        out = []
        for i in dict.fromkeys(ids or []):
            if not aid or not isinstance(i, str) or i == cid:
                continue
            try:
                v = self._external(i) if i.startswith("x:") else self.chat_view(i)
            except Problem:
                continue
            mine = share.of_agent(agents.get(aid)) == acct if i.startswith("x:") else owners.get(i) == acct
            if v.get("agent") == aid and mine:
                out.append({"id": i, "title": v.get("title") or "对话"})
        return out[:10]

    async def send(self, cid, text, files=(), ctx=None, refs=None, hidden=None, mark=None):
        """hidden：接在用户这句话后面交给助手、不进记录的内容（复盘的材料）；mark：记在用户这条记录上的标记。"""
        b = self._bridge(cid)
        m = self.meta(cid)
        refs = self._refs(cid, refs)
        if m.get("fork_from") and cid not in b.chats:
            b.chat(cid, "p2p", None if m.get("fork_at") else m["fork_from"])
        self._back_from_away(cid, m)
        head = re.match(r"(?:>[^\n]*\n)*\s*", text or "").end()   # 网页的引用拼在最前面，跳过它再认（#117）
        rv = board_mod.COMMAND.match((text or "")[head:])
        if rv:                                # /review 要求（#364）：新开的对话记成复盘对话，拉过哪几天的材料就列在任务面板那几天里
            ask = text[head + rv.end():].strip()
            chats = self.chats()
            if cid in chats and not chats[cid].get("title"):
                chats[cid].update(review=True, title=title_text(ask)[:30] or "复盘", title_auto=True)
                self.store.write(chats)
        mid = "u-" + uuid.uuid4().hex[:10]
        shown = [{"name": f["name"], "size": f.get("size"), "path": os.path.realpath(f["key"])} for f in files]
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "id": mid, "text": text, "files": shown,
                                                               **({"refs": refs} if refs else {}), **(mark or {})}))
        if text and not m.get("title"):       # 用用户的原话当标题：交给助手的会拼上引用说明等前缀
            chats = self.chats()
            if cid in chats and not chats[cid].get("title"):
                chats[cid]["title"] = title_text(text)[:30]
                self.store.write(chats)
        if rv:                                # Claude Code 自己也有 /review（审代码），不能原样交过去
            text = text[:head] + (ask or "复盘今天的工作。")
            hidden = board_mod.note() + (f"\n\n{hidden}" if hidden else "")
        from .bridge import QUEUE
        q = QUEUE.match((text or "")[head:])   # 前面还要拼别的内容，所以 /queue 在这里认出来
        if q:
            text = text[:head] + text[head + q.end():]
        if not m.get("mention_of"):           # 刚才 @ 的 Agent 的回答，原来的助手还没看过：带上；群聊说明改过也带上
            from . import groups
            text = groups.note_prefix(self, cid, groups.MAIN) + self.mentions.unseen(cid) + self._doc_edits(cid) + text if text else text
        if m.get("side_of") and text:          # 侧边对话：带上主对话的新进展
            text = self._side_ctx(cid, m) + text
        if refs and text:                     # 用户指了以前的对话：告诉它在哪读，不用自己搜
            from .search import short
            text = "（用户指了你以前的这些对话，和这次说的有关，用 open_chat 读，不用再搜：" + \
                "；".join(f"「{r['title']}」id={short(r['id'])}" for r in refs) + "）\n\n" + text
        if ctx and m.get("helper") and text:      # 助手要知道用户在页面上正看着什么（不记进对话记录）
            text = f"（{str(ctx)[:300]}）\n{text}"
        elif isinstance(ctx, dict) and isinstance(ctx.get("ws_file"), str) and text and self.ws_in(cid, ctx["ws_file"]):
            text = f"（我正在工作区里看这个文件：{ctx['ws_file']}）\n{text}"     # 工作区（#206）
        if hidden and text:
            text = f"{text}\n\n{hidden}"
        inc = Incoming(chat_id=cid, chat_type="p2p", sender_id=OWNER, message_id=mid, text=text,
                       files=[("file", f["key"], f["name"]) for f in files], kind="text" if text else "file", queue=bool(q))
        await b.on_message(inc)

    def diary(self, cid, text):
        """/diary 内容（#363）：记进日记（存日记的那台电脑，#376），对话里留一张日记卡片（type=diary），不交给助手、不进它的上下文。
        在请求线程里调：转给别的电脑要等，不占主循环。"""
        from . import diary as diary_mod
        content = diary_mod.parse(text) or ""

        def prep():
            if not cid:                       # 新对话页里记的（#381）：不属于哪个对话
                return "", "", False, None
            try:
                v = self._external(cid) if (cid or "").startswith("x:") else self.chat_view(cid)
            except Problem:
                v = {}
            title = v.get("title") or ""
            where = f"{v['agent_name']}「{title}」" if v.get("agent_name") and title else f"「{title}」" if title else ""
            rid = "d-" + uuid.uuid4().hex[:10]
            logged = cid in self.chats()
            return where, rid, logged, self.diary_link(cid, v, rid if logged else None)
        where, rid, logged, link = self.call(prep)
        r = self.diary_write(content, where, link=link)
        if logged:
            self.call(lambda: self.channel._emit(cid, self.channel.logs.append(cid, {"type": "diary", "id": rid, "text": content})))
        out = {"ok": True, "diary": True, "chat": cid}
        if r.get("pending"):
            out.update(pending=True, machine=r.get("machine"))
        return out

    def _machine_name(self, mid):
        return next((m["name"] for m in self.peers.machines() if m["id"] == mid), "存日记的电脑")

    def diary_write(self, text, where, at=None, link=None, here=False):
        """记一条到存日记的那台电脑（#376）：是这台（或 here：别的电脑转来的）就写，否则转过去；
        那台不在线先存在本机（diary.hold），连上后按原来的时间补写。返回 {date, time}，暂存的带 pending、machine。"""
        from . import diary as diary_mod
        from .peers import Offline
        now = diary_mod.when(at)
        home = self.peers.diary_home()
        if here or home == self.peers.me["id"]:
            if link and link.startswith("http://127.0.0.1:"):    # 别的电脑上的本机网址：换成这台的，在这台上点得开
                link = self._web_root() + link[link.find("/", len("http://")):]
            try:
                r = diary_mod.add(text, where, now, link)
            except ValueError as e:
                raise Problem(str(e))
            except OSError:
                raise Problem("日记没记上：文稿文件夹写不进去。")
            return {"date": r["date"], "time": r["time"]}
        if not (text or "").strip():
            raise Problem("在 /diary 后面写上要记的内容。")
        item = {"text": text, "where": where, "at": now.isoformat(timespec="seconds"), "link": link}
        try:
            if not home:                          # 还不知道存在哪台（刚加入服务器上的总控）：先存着
                raise Offline()
            st, _, data = self.peers.forward(home, "POST", "/api/diary/add", json.dumps(dict(item, here=True)).encode(),
                                             {"Content-Type": "application/json"})
        except Offline:
            st, data = 503, b"{}"
        if st == 200:
            return json.loads(data)
        if st in (404, 502, 503):                 # 不在线、还没升级到能收日记的版本：先存着
            diary_mod.hold(item)
            return {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "pending": True,
                    "machine": self._machine_name(home)}
        try:
            msg = json.loads(data).get("error")
        except ValueError:
            msg = None
        raise Problem(msg or "日记没记上，再试一次。")

    def diary_query(self, op, args, here=False):
        """Agent 查日记（#376）：去存日记的那台查。那台不在线时说清楚查不到。"""
        from . import diary as diary_mod
        from .peers import Offline
        home = self.peers.diary_home()
        args = {k: args.get(k) for k in ("start", "end", "query")}
        if here or home == self.peers.me["id"]:
            return {"text": diary_mod.run_query(op, args)}
        if not home:
            raise Problem("还不知道日记存在哪台电脑，等这台电脑连上总控再查。")
        name = self._machine_name(home)
        try:
            st, _, data = self.peers.forward(home, "POST", "/api/diary/query", json.dumps(dict(args, op=op, here=True)).encode(),
                                             {"Content-Type": "application/json"})
        except Offline:
            raise Problem(f"日记存在「{name}」上，它现在没连上，查不到。")
        try:
            r = json.loads(data)
        except ValueError:
            r = {}
        if st == 200 and isinstance(r.get("text"), str):
            return {"text": r["text"]}
        if st == 404:
            raise Problem(f"日记存在「{name}」上，那台的 ccim 版本较旧，先升级再查。")
        raise Problem(r.get("error") or f"日记存在「{name}」上，现在查不到。")

    def diary_home_view(self):
        """设置页：日记存在哪台电脑，能选哪几台。"""
        mid = self.peers.diary_home()
        ms = [{"id": m["id"], "name": m["name"], "online": m.get("online", True)} for m in self.peers.machines() if not m.get("shared")]
        return {"machine": mid, "machines": ms}

    def _diary_flush_loop(self):
        """暂存的日记（存日记的电脑当时不在线）：连上了就按原来的时间补写（#376）。"""
        from . import diary as diary_mod
        while True:
            time.sleep(15)
            try:
                home = self.peers.diary_home()
                if not home or not self.peers.online(home) or not os.path.exists(diary_mod._pending_file()):
                    continue
                for item in diary_mod.take_pending():
                    try:
                        r = self.diary_write(item.get("text") or "", item.get("where") or "", item.get("at"), item.get("link"))
                    except Problem:
                        log.warning("暂存的日记写不进去，丢掉：%s", item.get("at"))
                        continue
                    except Exception:
                        log.warning("补写日记出错", exc_info=True)
                        diary_mod.hold(item)
                        continue
                    if not r.get("pending"):
                        log.info("补写了 %s 的日记", item.get("at"))
            except Exception:
                log.warning("补写日记出错", exc_info=True)

    def _web_root(self):
        return f"http://127.0.0.1:{registry.read_state(self.home).get('port') or self.entry.get('port') or PORT}"

    def diary_link(self, cid, v, rid):
        """日记里回到记它的地方的完整网址（#363）：打开那个对话、滚到那张日记卡片（rid，按记录 id 找，退回删掉了就只打开对话）。
        网址的根：加入了服务器上的总控用它的地址（哪里都能打开），否则用本机网页。Skill 助手里记的不带；侧边对话指向主对话。"""
        m = self.meta(cid) if not cid.startswith("x:") else {}
        if m.get("helper"):
            return None
        if m.get("side_of"):
            cid, rid, v = m["side_of"], None, {"agent": self.meta(m["side_of"]).get("agent")}
        hub = (getattr(getattr(self, "peers", None), "hub", None) or {}).get("url") or ""
        root = hub if hub.startswith("https://") else self._web_root()
        aid = v.get("agent")
        path = f"#/agents/{quote(aid, safe='')}/{quote(cid, safe='')}" if aid else f"#/chats/{quote(cid, safe='')}"
        return f"{root.rstrip('/')}/{path}" + (f"?m={rid}" if rid else "")

    def review_range(self, cid, start, end):
        """复盘对话（#364）拉了 start～end 的材料：记进它覆盖的日期范围，任务面板那几天里列出它。"""
        chats = self.chats()
        m = chats.get(cid)
        if not m or not m.get("review"):
            return
        r = board_mod.merged(m, start, end)
        if r != m.get("review"):
            m["review"] = r
            self.store.write(chats)

    async def helper_chat(self, kind, fresh=False, account=None):
        """页面专用的助手对话（#116，现在只有 Skill 页）：每台电脑一个（共享电脑上的访客各自一个），不进对话列表；fresh 就换一个新的。"""
        if kind not in HELPERS:
            raise Problem("没有这个助手。")
        old = [cid for cid, m in self.chats().items() if m.get("helper") == kind and (m.get("account") or None) == account]
        if old and not fresh:
            return old[0]
        for cid in old:
            try:
                await self.stop_chat(cid)
                await self.delete_chat(cid)
            except Problem:
                pass
        cid = self.new_chat(temp={"base": "claude", "dir": share.root(account) if account else None},
                            title=HELPERS[kind]["title"], account=account)
        chats = self.chats()
        chats[cid]["helper"] = kind
        self.store.write(chats)
        return cid

    def pocket_chat(self, account=None, fresh=False, base=None):
        """右下角的助理（#294）：每个账号接着用最近那个；fresh 或换了底座（会话不通用）就新开一个，老的照常留在助理列表里。
        新开的用设置里的默认底座、模型、思考深度；换成别的底座时模型、思考深度跟随全局默认。"""
        g = pocket_settings()
        mine = [(m.get("created") or "", cid) for cid, m in self.chats().items()
                if m.get("pocket") and not m.get("archived") and (m.get("account") or None) == account]
        cur = max(mine)[1] if mine else None
        base = base if base in ("claude", "codex") else None
        if cur and not fresh and (not base or (self.meta(cur).get("temp") or {}).get("base") == base):
            return cur
        empty = bool(cur) and not self.channel.logs.read(cur) and not self.meta(cur).get("busy")
        if empty and (not base or (self.meta(cur).get("temp") or {}).get("base") == base):
            return cur                        # 还没开口：就用它，不另开
        base = base or g["base"]
        same = base == g["base"]
        if empty:
            self.store.deleted.add(cur)           # 还没开口的那个不留着，免得列表里一串空对话
            chats = self.chats()
            chats.pop(cur, None)
            self.store.write(chats)
            self.bridges.pop("temp:" + cur, None)
        root = share.root(account) if account else None
        cid = self.new_chat(temp={"base": base, "dir": root, "model": g["model"] if same else None,
                                  "effort": g["effort"] if same else None}, account=account)
        chats = self.chats()
        chats[cid]["pocket"] = True
        if not root:
            chats[cid]["temp"]["scratch"] = "pocket"   # 几个助理对话共用一个临时目录，前面做出来的文件后面还找得到
        self.store.write(chats)
        return cid

    async def side_chat(self, cid, fresh=False):
        """侧边对话（#196）：这个对话的侧边对话，一个对话一个，不进对话列表；没有或 fresh 就从主对话最后一条回答处分一个新的。"""
        m = self.meta(cid)
        if m.get("side_of") or m.get("helper") or m.get("mention_of") or cid.startswith("x:"):
            raise Problem("这个对话不能开侧边对话。")
        old = [c for c, x in self.chats().items() if x.get("side_of") == cid]
        if old and not fresh:
            return old[0]
        for c in old:
            await self._drop_side(c)
        rows = self.channel.logs.read(cid)
        base = self._effective(m)["base"]
        sid = m.get("codex_session_id" if base == "codex" else "session_id")
        done = [i for i, r in enumerate(rows) if r.get("type") == "answer"]
        upto, fork, at = 0, None, None
        if sid and base == "codex":
            if not m.get("busy"):             # Codex 只能从整个对话分；正在写的那一轮不分进来
                fork, upto = sid, len(rows)
        elif sid and done:
            f = rows[done[-1]].get("fork") or {}
            if f.get("at"):
                fork, at, upto = f.get("sid") or sid, f["at"], done[-1] + 1
        t = m.get("temp")
        new = self.new_chat(agent=m.get("agent"), temp=dict(t) if t else None, title="侧边对话",
                            account=share.chat_owner(self, cid))
        chats = self.chats()
        rec = chats[new]
        rec.update(side_of=cid, side_upto=upto)
        if fork:
            rec.update(fork_from=fork, fork_at=at)
        if t and not t.get("dir"):            # 没选目录的临时对话：用原来那个临时目录（会话记录按目录存）
            rec["temp"]["scratch"] = t.get("scratch") or cid
        for k in ("cwd", "kbs"):
            if m.get(k):
                rec[k] = m[k]
        self.store.write(chats)
        return new

    async def _drop_side(self, c):
        try:
            await self.stop_chat(c)
            self._bridge(c).save(c, busy=False)  # 刚停下，这一轮的收尾可能还没写回来
        except Problem:                       # Agent 已经删了
            pass
        await self.delete_chat(c)

    def _side_ctx(self, cid, m):
        """侧边对话每次发消息时带上的：主对话在上次之后的新内容，和正在进行的那一轮做到哪了。"""
        of = m["side_of"]
        rows = self.channel.logs.read(of)
        upto = m.get("side_upto") or 0
        parts = []
        for r in rows[upto:]:
            t = " ".join((r.get("text") or "").split())
            if r.get("type") == "user" and t:
                parts.append(f"用户：{t[:800]}")
            elif r.get("type") == "answer" and t:
                parts.append(f"回答：{t[:1500]}{'…' if len(t) > 1500 else ''}")
        now = self.live_of(of)
        if now:
            parts.append(now)
        chats = self.chats()
        if cid in chats:
            chats[cid]["side_upto"] = len(rows)
            self.store.write(chats)
        if not parts:
            return ""
        body = "\n\n".join(parts)
        if len(body) > SIDE_CTX:
            body = "…" + body[-SIDE_CTX:]
        return f"（平台自动附上的主对话最新进展）\n{body}\n\n——\n\n"

    def live_of(self, cid):
        """这个对话正在进行的那一轮做到哪了，整理成文字；没在干活返回空。"""
        p = self.channel.progress_of.get(cid)
        if not p:
            return ""
        steps = p.get("steps") or []
        now = "正在进行的这一轮：" + ("已经做了 " + "；".join(steps[-15:]) if steps else "刚开始")
        if (p.get("text") or "").strip():
            now += "\n正在写的回答：" + p["text"].strip()[-1500:]
        return now

    def work(self, src, text, title=None):
        """被 @ 的 Agent 要动手：新开一个它自己的对话去做。侧边对话里被 @ 的（#290）、群聊里被 @ 的成员（#359）都走这里。
        卡片（type=work）记在 @ 它的那个对话里（侧边对话 / 群聊），做完由它在那里汇报（Mentions.report），文件附在汇报下面。
        新对话的 dispatched_from 指主对话（侧边对话的主对话 / 群聊本身），同一个 Agent 时挂在它下面。返回 (新对话, 标题)。在主循环里调。"""
        from .session import Task
        sm = self.meta(src)
        outer = sm.get("mention_of")          # @ 它的对话：卡片、汇报都在这里
        od = self.chats().get(outer) or {}
        aid = sm.get("agent")
        side = bool(sm.get("side_at"))
        of = od.get("side_of") if side else outer
        if not outer or not od or not of or sm.get("remote_project") or not agents.get(aid) or (od.get("side_of") and not side):
            raise Problem("这里不能新开对话去做。")
        om = self.chats().get(of) or {}
        title = " ".join((title or text).split())[:30] or "新任务"
        cid = self.new_chat(agent=aid, title=title, account=od.get("account"),
                            parent=of if om.get("agent") == aid else None)
        chats = self.chats()
        chats[cid]["dispatched_from"] = of
        if sm.get("cwd") and sm["cwd"] != agents.cwd(agents.get(aid)):   # 在主对话的目录里看的，就在那里做
            chats[cid]["cwd"] = sm["cwd"]
        self.store.write(chats)
        main = (self.chat_view(of).get("title") if of in self.chats() else None) or "另一个对话"
        place = "侧边对话" if side else "群聊"
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "id": "u-" + uuid.uuid4().hex[:10], "text": text,
                                                               "files": [], "from_chat": of, "dispatch": True}))
        wid = "k-" + uuid.uuid4().hex[:10]
        card = {"type": "work", "id": wid, "chat": cid, "agent": aid, "title": title, "state": "working"}
        self.channel._emit(outer, self.channel.logs.append(outer, card))
        head = (f"[用户在对话「{main}」的侧边对话里请你动手做这件事" if side else f"[你在群聊「{main}」里被 @，要动手做这件事") + \
            f"，新开这个对话来做；做完把结果写清楚（做了什么、怎么验收），会发回{place}] "
        task = Task(head + text)
        task.depth = 1
        start = time.time()
        self._bridge(cid).chat(cid, "p2p").submit(task)
        self.works.add(wid)
        chats = self.chats()
        if outer in chats:
            chats[outer]["last_active"] = registry.now()
            self.store.write(chats)

        async def wait():
            try:
                state, answer = await task.future
            except Exception as e:
                state, answer = "error", str(e)
            rows = [r for r in self.channel.logs.read(cid) if r.get("at", 0) >= start]
            last = next((r for r in reversed(rows) if r.get("type") == "answer"), {})
            files = [{"name": r["name"], "path": r["path"], "size": r.get("size")} for r in rows if r.get("type") == "file"][-10:]
            state = {"ok": "done"}.get(state, state)
            text = answer or last.get("text") or ""
            self.works.discard(wid)
            if outer not in self.chats():
                return
            self.channel._emit(outer, self.channel.logs.append(outer, dict(card, state=state, done_at=time.time())))
            # 卡片只管进度；结果由它自己回去汇报，之后接着问也接得上
            how = {"done": "做完了", "error": "出错了", "stopped": "被停下了"}.get(state, "没做完")
            att = "".join(f"\n- {f['name']}（{f['path']}）" for f in files)
            self.mentions.report(outer, aid, (
                f"[你在{place}里新开的对话「{title}」{how}。下面是那边最后的回答" + ("和发出的文件" if files else "") +
                f"。请在{place}里用你的口吻向用户汇报：结果是什么、改了什么、PR 或文件在哪、要用户做什么（验收、拍板）。"
                + ("这些文件会附在你这条消息下面，不用再发。" if files else "") +
                "简短，不用复述全文；用户想看细节可以点卡片进那个对话。]\n\n"
                + (text[:8000] or "（没有回答）") + (f"\n\n发出的文件：{att}" if files else "")),
                {"files": files, "files_chat": cid} if files else {})
        t = asyncio.ensure_future(wait())
        self.background.add(t)
        t.add_done_callback(self.background.discard)
        return cid, title

    # ----- 对话之间转交（#121） -----

    async def relay(self, src, sid, text, wait=0):
        """把 text 交给会话 sid 所在的那个对话（同一个 Agent 的网页对话，或者终端、桌面端里的 Claude Code 对话），
        排在它手头这一轮后面。wait 秒内等它回答完就返回 (state, 回答)，否则 ("sent", "")。
        终端、桌面端正开着这个对话时不写进去（两边同时写会分叉），返回 ("open", 在哪开着)。"""
        from . import history
        from .session import Task
        sm = self.chats().get(src) or {}
        if sm.get("mention_of"):              # 群聊里被 @ 的成员：来源记成那个群聊（子对话不进列表）
            src = sm["mention_of"]
        target = next((cid for cid, m in self.chats().items()
                       if m.get("session_id") == sid and cid != src
                       and not (m.get("mention_of") or m.get("project") or m.get("schedule"))), None)
        if not target:
            try:
                r = self._external("x:" + sid)
            except Problem:
                raise Problem("找不到这个对话。")
            if r.get("source") != "终端" or r.get("base") != "claude":
                raise Problem(f"{r.get('source')}里的对话在那边的进程里，转交不了，请用户到那边说。")
            if sm.get("agent") and r.get("agent") != sm.get("agent"):
                raise Problem("只能转交给同一个 Agent 的对话；要和别的 Agent 协作，请用户拉群聊。")
            where = history.open_elsewhere(sid)
            if where:
                return "open", where
            target = self.new_chat(agent=r["agent"], link=sid, title=r["title"], source=r["source"])
        elif sm.get("agent") and self.chats()[target].get("agent") != sm.get("agent"):
            raise Problem("只能转交给同一个 Agent 的对话；要和别的 Agent 协作，请用户拉群聊。")
        elif self._linked(self.meta(target)) and history.open_elsewhere(sid):   # 接着终端对话聊的网页对话，那边又开着
            return "open", history.open_elsewhere(sid)
        title = (self.chat_view(src).get("title") if src in self.chats() else None) or "另一个对话"
        mid = "u-" + uuid.uuid4().hex[:10]
        self.channel._emit(target, self.channel.logs.append(target, {"type": "user", "id": mid, "text": text, "files": [],
                                                                      "from_chat": src, "relay": True}))
        chats = self.chats()
        if target in chats:
            chats[target]["last_active"] = registry.now()
            self.store.write(chats)
        task = Task(f"[用户在这个 Agent 的另一个对话「{title}」里，让那边的你把下面这段话转交过来，请照常处理] {text}")
        task.relayed = True                   # 它处理这条时不能再往外转交，免得来回停不下来
        self._bridge(target).chat(target, "p2p").submit(task)
        if not wait:
            return "sent", target
        try:
            state, answer = await asyncio.wait_for(asyncio.shield(task.future), wait)
        except asyncio.TimeoutError:
            return "waiting", target
        return {"ok": "done"}.get(state, state), answer

    def dispatch_self(self, src, text, depth):
        """对话里的助手给自己派活（ask_agent 发给自己，#179）：新开一个同样设置的对话去做，挂在发起的对话下面。
        返回 (新对话, Task)。在主循环里调。"""
        from .session import Task
        sm = self.meta(src)
        outer = sm.get("mention_of") or src   # 群聊里被 @ 的成员：来源记成那个群聊（子对话不进列表）
        om = self.chats().get(outer) or {}
        aid, t = sm.get("agent"), sm.get("temp")
        cid = self.new_chat(agent=aid, temp=dict(t) if t else None, title=" ".join(text.split())[:30] or "派出的任务",
                            account=sm.get("account"), parent=outer if om.get("agent") == aid else None)
        chats = self.chats()
        chats[cid]["dispatched_from"] = outer
        if t and not t.get("dir"):            # 没选目录的临时对话：用发起的那个的临时目录，看得到它做出来的文件
            chats[cid]["temp"]["scratch"] = t.get("scratch") or src
        self.store.write(chats)
        title = (self.chat_view(outer).get("title") if outer in self.chats() else None) or "另一个对话"
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "id": "u-" + uuid.uuid4().hex[:10], "text": text,
                                                               "files": [], "from_chat": outer, "dispatch": True}))
        task = Task(f"[你在另一个对话「{title}」里给自己派了这件事，新开这个对话来做；做完把结果写清楚，会交回那边] {text}")
        task.depth = depth
        self._bridge(cid).chat(cid, "p2p").submit(task)
        return cid, task

    def deliver(self, cid, text):
        """把一段话作为新的一轮交给对话 cid（派出去的活做完了，结果交回发起的对话，#179）。不记成用户说的。"""
        from .session import Task
        if cid not in self.chats():
            return False
        self._bridge(cid).chat(cid, "p2p").submit(Task(text))
        return True

    async def skip_step(self, cid):
        """结束卡住的那一步，接着做（#109）。"""
        c = self._bridge(cid).chats.get(cid)
        return bool(c and await c.skip())

    def _stall(self, cid):
        """这个对话（群聊连同里面被 @ 的成员）卡住的那一步：{step, secs, chat}。"""
        p = self.channel.progress_of
        hits = [dict(v["stall"], chat=k) for k, v in list(p.items())
                if (k == cid or k.startswith(f"m-{cid}-")) and v.get("stall")]
        return max(hits, key=lambda x: x["secs"]) if hits else None

    async def stop_chat(self, cid):
        hit = await self.mentions.stop_all(cid)
        b = self._bridge(cid)
        c = b.chats.get(cid)
        return bool(c and await c.stop()) or hit

    async def action(self, cid, mid, value):
        b = self._bridge(cid)
        card, toast = await b.on_action(CardAction(chat_id=cid, message_id=mid, operator_id=OWNER, value=value or {}))
        if card:
            await self.channel.update_card(mid, card)
        return {"toast": toast}

    async def set_chat(self, cid, key, value):
        if key not in ("model", "effort"):
            raise Problem("只能改模型和思考深度。")
        b = self._bridge(cid)
        c = b.chat(cid, "p2p")
        if value:
            c.set(**{key: value})
        else:
            c.follow(key)
        return self.chat_view(cid)

    async def delete_chat(self, cid):
        chats = self.chats()
        if cid in chats and chats[cid].get("busy"):
            raise Problem("它还在干活，先停下再删。")
        await self.mentions.delete_subs(cid)
        for c in [c for c, x in chats.items() if x.get("side_of") == cid]:   # 它的侧边对话一起删
            await self._drop_side(c)
        self.store.deleted.add(cid)
        for key, b in list(self.bridges.items()):
            c = b.chats.pop(cid, None)
            if c:
                await c.stop()
                if c.worker:
                    c.worker.cancel()
            if key == "temp:" + cid:
                self.bridges.pop(key, None)
        self.store.write(self.chats())
        self.channel.logs.remove(cid)

    def delete_terminal(self, cid):
        """终端里的 Claude Code 对话：连对话记录一起删（#61）。飞书、微信里的还在被机器人用，不删。"""
        r = self._external(cid)
        if r.get("source") != "终端" or r.get("base") != "claude":
            raise Problem("只有终端里的对话能一起删除。")
        from claude_agent_sdk import delete_session
        try:
            delete_session(r["sid"], directory=agents.cwd(agents.get(r["agent"])))
        except FileNotFoundError:
            pass
        self.mark_chat(cid, {"hidden": True})
        getattr(self, "_ext_seen", {}).pop(cid, None)

    # ----- 给网页的数据 -----

    def _effective(self, m, c=None):
        """这个对话实际用的底座、模型、思考深度（和是不是跟随默认）。"""
        a = agents.get(m["agent"]) if m.get("agent") else None
        t = m.get("temp") or {}
        base = (a or {}).get("base") or t.get("base") or "claude"
        g = registry.global_config()
        px = "" if base == "claude" else "codex_"
        c = c or {}
        keep = c.get("keep") or {}            # 改 Agent 设置时选了只用于新对话：留着当时的（#387）
        model = c.get("model") or (keep["model"] if "model" in keep else (a or t).get("model")) or g.get(px + "model")
        effort = c.get("effort") or (keep["effort"] if "effort" in keep else (a or t).get("effort")) or g.get(px + "effort")
        dm, de = _defaults(base, agents.cwd(a) if a else (t.get("dir") or os.path.expanduser("~")))
        return {"base": base, "model": model or dm, "effort": effort or de,
                "model_custom": bool(c.get("model") or keep.get("model")), "effort_custom": bool(c.get("effort") or keep.get("effort"))}

    def _fork_title(self, cid, m):
        """分叉出来、还没开口的对话：用分叉那条回答之前的问题当标题；开口后第一句话就是标题。"""
        if not m.get("parent"):
            return None
        q = next((r.get("text") for r in reversed(self.channel.logs.read(cid)) if r.get("type") == "user" and r.get("text")), "")
        q = title_text(q)
        return f"从「{q[:16]}{'…' if len(q) > 16 else ''}」分出" if q else None

    def chat_view(self, cid):
        m = self.meta(cid)
        a = agents.get(m["agent"]) if m.get("agent") else None
        t = m.get("temp") or {}
        return {"id": cid, "title": m.get("title") or self._fork_title(cid, m) or "新对话", "agent": m.get("agent"),
                "agent_name": a["name"] if a else None, "temp": bool(not m.get("agent")),
                "dir": m.get("cwd") or (a.get("dir") if a else t.get("dir")), "busy": bool(m.get("busy")),
                "mention_of": m.get("mention_of"), "side_of": m.get("side_of"),
                "split_from": {"id": m["split_from"], "title": (self.chats().get(m["split_from"]) or {}).get("title") or "群聊"}
                if m.get("split_from") else None,
                "dispatched_from": {"id": m["dispatched_from"], "title": (self.chats().get(m["dispatched_from"]) or {}).get("title") or "另一个对话"}
                if m.get("dispatched_from") else None,
                "members": [a for a in m.get("members") or [] if a != m.get("agent")], "note": m.get("note") or "",
                "source": f"项目「{m.get('title')}」" if m.get("project") else f"定时任务「{m.get('title')}」" if m.get("schedule")
                else "群聊" if m.get("mention_of") else m["source"] if self._linked(m) else "网页", "project": m.get("project"), "schedule": m.get("schedule"),
                "listed": listed(cid, m), "pocket": bool(m.get("pocket")), "review": bool(m.get("review")), "review_span": board_mod.span(m),
                "origin_source": m.get("source"),
                "parent": m.get("parent") or ("x:" + m["origin"] if m.get("origin") and not self._linked(m) else None),
                **{k: m.get(k) for k in MARKS if k != "title"}, "delete_at": delete_at(m, archive_days()), "kbs": m.get("kbs") or [],
                **self._board_marks(cid, m), "issue": self._issue(cid, m),
                "brief": m.get("brief"), "doing": ((self.channel.progress_of.get(cid) or {}).get("steps") or [None])[-1],
                "stall": self._stall(cid), "bg": self.channel.bg_of.get(cid) or [],
                "last_active": m.get("last_active"), "created": m.get("created"), "ctx": m.get("ctx"),
                "member_ctx": self.mentions.member_ctx(cid, m), **self._effective(m, m)}

    @staticmethod
    def _linked(m):
        """接着终端、桌面端的对话聊，而且还在用原来那个会话（没退回、没因为接不上换成新会话）。"""
        return bool(m.get("continued_at") and m.get("origin") and m.get("session_id") == m["origin"])

    def chat_detail(self, cid):
        if cid.startswith("x:"):
            return self.external_detail(cid)
        v = self.chat_view(cid)
        v["log"] = self.mentions.lost(self.channel.logs.read(cid))
        v["rewind"] = self.rewindable(cid, v["log"])
        v["chapters"] = self.meta(cid).get("chapters") or {}
        for r in v["log"]:                    # 平台重启过：没处理的密码卡片作废
            if r.get("type") == "secret" and r.get("state") == "pending" and r["id"] not in self.channel.secrets:
                r.update(state="expired")
            elif r.get("type") == "work" and r.get("state") == "working" and r["id"] not in self.works:
                r.update(state="lost")
        v["progress"] = self.channel.progress_of.get(cid)
        try:
            v["ws_root"] = os.path.realpath(self.chat_dir(cid))
        except Problem:
            v["ws_root"] = None
        v["sub_progress"] = {r["chat"]: self.channel.progress_of[r["chat"]] for r in v["log"]
                             if r.get("type") == "mention" and r.get("chat") in self.channel.progress_of}
        m = self.meta(cid)
        if m.get("origin"):
            from . import history
            a = agents.get(m["agent"]) if m.get("agent") else None
            cwd = agents.cwd(a) if a else None
            rows = history.read(v["base"], m["origin"], cwd)
            if m.get("continued_at"):         # 接着原来的会话聊：分隔线以上是转过来之前的，之后别处说的穿插显示
                at = m["continued_at"]
                v["earlier"] = [r for r in rows if (r.get("at") or 0) < at]
                v["elsewhere"] = self._elsewhere([r for r in rows if (r.get("at") or 0) >= at], v["log"]) \
                    if self._linked(m) else []
                if m.get("busy"):             # 正在答的这一轮还没对上号，先不当成别处说的
                    asked = max([r.get("at") or 0 for r in v["log"] if r.get("type") == "user"] or [0])
                    v["elsewhere"] = [r for r in v["elsewhere"] if (r.get("at") or 0) < asked - 1]
                if self._linked(m):
                    v["linked"] = True
                    v["open_elsewhere"] = history.open_elsewhere(m["origin"])
                    v["sig"] = history.sig(v["base"], m["origin"], cwd)
            else:
                v["earlier"] = rows
        elif m.get("away"):                   # 转到微信、飞书接着聊过（#212）：那边说的从同一个会话的记录里读
            from . import history
            a = agents.get(m["agent"]) if m.get("agent") else None
            cwd = agents.cwd(a) if a else None
            sid = m.get("codex_session_id" if v["base"] == "codex" else "session_id")
            away = m["away"]
            rows = history.read(v["base"], sid, cwd) if sid else []
            if v["base"] == "codex":          # Codex 的记录没有消息编号，认不出哪些是网页说的：只取在那边的那几段时间
                rows = [r for r in rows if any(x["at"] <= (r.get("at") or 0) < (x.get("back") or float("inf")) for x in away)]
            ew = self._elsewhere([r for r in rows if (r.get("at") or 0) >= away[0]["at"]], v["log"])
            if m.get("busy"):
                asked = max([r.get("at") or 0 for r in v["log"] if r.get("type") == "user"] or [0])
                ew = [r for r in ew if (r.get("at") or 0) < asked - 1]
            for r in ew:                      # 每条是在哪边说的：它之前最近的那次转过去
                r["via"] = next((x["to"] for x in reversed(away) if (r.get("at") or 0) >= x["at"]), away[0]["to"])
            v["elsewhere"], v["away"] = ew, True
            v["sig"] = history.sig(v["base"], sid, cwd) if sid else None
        return v

    @staticmethod
    def _elsewhere(rows, log):
        """原来的会话里、转到网页之后的内容，去掉网页自己说的：剩下的就是在终端、桌面端里接着说的。"""
        mine = set()
        for r in log:
            if r.get("type") == "answer":
                mine |= {r.get("prompt"), (r.get("fork") or {}).get("at")}
        mine.discard(None)
        out, skip = [], False
        for r in rows:
            if r["type"] == "user":
                skip = r.get("uuid") in mine
            elif r.get("uuid") in mine:
                skip = True
            if not skip:
                out.append(dict(r, elsewhere=True))
        return out

    # ----- 对话列表：整体状态里只带一部分，其余按页取（#268） -----
    # 对话一直在涨，每次都给全部对话生成列表数据、整份传给网页太重。先用记录里现成的字段挑出要的，只给挑中的生成。

    def _tops(self, ok):
        """列表里的对话，新的在前：[(最后活跃, 编号, 网页对话的记录 或 None, 飞书 / 微信 / 终端里那行 或 None)]。"""
        out = [(m.get("last_active") or "", cid, m, None) for cid, m in self.chats().items()
               if board_mod.top_level(m, cid) and ok(cid, m.get("agent"))]
        out += [(r.get("last_active") or "", r["id"], None, r) for r in self.external_chats() if ok(r["id"], r.get("agent"))]
        out.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return out

    @staticmethod
    def _archived(t):
        return bool((t[2] if t[2] is not None else t[3]).get("archived"))

    def _hot(self, t):
        """一直带在整体状态里的：置顶、星标、在干活 / 卡住 / 群里有人在干活、等你处理、没看过的回答。"""
        m = t[2] if t[2] is not None else t[3]
        if m.get("pin") is not None or m.get("busy"):
            return True
        if t[2] is None:
            return False
        if self._stall(t[1]) or self.mentions.crew(t[1]):
            return True
        summ = board_mod.summary(self.channel.logs, t[1])
        return bool(board_mod.attention(m, summ) or board_mod.unread(m, summ))

    def _parent(self, t):
        if t[2] is None:
            return t[3].get("parent")
        m = t[2]
        return m.get("parent") or ("x:" + m["origin"] if m.get("origin") and not self._linked(m) else None)

    def _row(self, t):
        return t[3] if t[3] is not None else self.chat_view(t[1])

    @staticmethod
    def _listed(t):
        """出现在「对话」列表里的：和 Agent 的对话在 Agent 页里列（走 /api/agent），不算在这里。"""
        return not (t[2] if t[2] is not None else t[3]).get("agent")

    def chat_head(self, acct=None):
        """整体状态里的对话：「对话」列表里最近 CHAT_PAGE 条（不含归档）、常驻的（和 Agent 的对话也算）、它们分叉前的原对话
        （分叉出来的挂在它下面）。另外给列表里更早的、已归档的各有多少，和翻下一页从哪开始。"""
        tops = self._tops(share.chat_filter(self, acct))
        listed = [t for t in tops if self._listed(t)]
        live = [t for t in listed if not self._archived(t)]
        keep = {t[1] for t in live[:CHAT_PAGE]} | {t[1] for t in tops if self._hot(t)}
        byid = {t[1]: t for t in tops}
        for cid in list(keep):
            seen = set()
            while cid in byid and cid not in seen:
                seen.add(cid)
                keep.add(cid)
                cid = self._parent(byid[cid])
        more = {"older": sum(1 for t in live[CHAT_PAGE:] if t[1] not in keep),
                "archived": sum(1 for t in listed if self._archived(t) and t[1] not in keep),
                "next": _cursor(live[CHAT_PAGE - 1]) if len(live) > CHAT_PAGE else None}
        return [self._row(t) for t in tops if t[1] in keep], more

    def chat_page(self, acct=None, before=None, archived=False, q="", groups=False):
        """/api/chats：「对话」列表里 before 之后（更早）的一页、已归档的一页、按标题和摘要搜；全部群聊。
        {rows, next}，next 是下一页从哪开始。"""
        tops = self._tops(share.chat_filter(self, acct))
        if groups:                            # 定时任务里选「发到群聊」
            return {"rows": [self._row(t) for t in tops if t[2] is not None and t[2].get("agent") and t[2].get("members")
                             and not self._archived(t)], "next": None}
        tops = [t for t in tops if self._listed(t)]
        if q:
            words = q.lower().split()
            hit = [t for t in tops if all(w in self._text(t) for w in words)]
            return {"rows": [self._row(t) for t in hit[:CHAT_PAGE]], "next": None}
        at = _uncursor(before) if before else None
        sel = [t for t in tops if self._archived(t) == bool(archived) and (at is None or (t[0], t[1]) < at)]
        return {"rows": [self._row(t) for t in sel[:CHAT_PAGE]], "next": _cursor(sel[CHAT_PAGE - 1]) if len(sel) > CHAT_PAGE else None}

    def _text(self, t):
        if t[2] is None:
            return (t[3].get("title") or "").lower()
        m = t[2]
        return " ".join([m.get("title") or self._fork_title(t[1], m) or "新对话", m.get("brief") or ""]).lower()

    # ----- 列表里的标记：重命名、置顶、星标、已完成、图标、颜色、从列表里去掉 -----

    def mark_chat(self, cid, changes):
        changes = {k: v for k, v in (changes or {}).items() if k in MARKS}
        if "icon" in changes and changes["icon"] not in ICONS + [None]:
            raise Problem("没有这个图标。")
        if "color" in changes and changes["color"] not in COLORS + [None]:
            raise Problem("没有这个颜色。")
        if "title" in changes:
            changes["title"] = " ".join((changes["title"] or "").split())[:60] or None
            changes["title_set"] = bool(changes["title"])      # 手动改过名，不再自动起标题（#67）
            changes["title_auto"] = None
        if "star" in changes:                 # 老网页还会发星标：就是置顶（#347）
            star = changes.pop("star")
            changes.setdefault("pin", True if star else None)
        if changes.get("pin") is True:
            changes["pin"] = -time.time()         # 新置顶的排在最上面
        if "archived" in changes:
            changes["archived_at"] = time.time() if changes["archived"] else None
        if cid.startswith("x:"):
            marks = registry.read_state(self.home).get("marks") or {}
            was = bool(marks.get(cid, {}).get("archived"))
            m = {**marks.get(cid, {}), **changes}
            marks[cid] = {k: v for k, v in m.items() if v not in (None, False, "")}
            registry.write_state(self.home, marks=marks)
            if "archived" in changes and bool(changes["archived"]) != was:
                self._done_mark(cid, bool(changes["archived"]), changes.get("archived_at"))
            return {}
        if "hidden" in changes:
            raise Problem("网页里的对话直接删除。")
        chats = self.chats()
        if cid not in chats:
            raise Problem("找不到这个对话。")
        was = bool(chats[cid].get("archived"))
        for k, v in changes.items():
            if v in (None, False, ""):
                chats[cid].pop(k, None)
            else:
                chats[cid][k] = v
        self.store.write(chats)
        if "archived" in changes and bool(changes["archived"]) != was:     # 标为已完成、取消已完成（#369）
            self._done_mark(cid, bool(changes["archived"]), changes.get("archived_at"))
        return self.chat_view(cid)

    def set_chapter(self, cid, key, name):
        """章节（#25）：key 是消息的「类型:时间」，name 为空就取消。"""
        if not isinstance(key, str) or not re.fullmatch(r"(user|answer|note):[\d.]+", key):
            raise Problem("找不到这条消息。")
        name = " ".join((name or "").split())[:40]
        if cid.startswith("x:"):
            marks = registry.read_state(self.home).get("marks") or {}
            ch = dict((marks.get(cid) or {}).get("chapters") or {})
        else:
            chats = self.chats()
            if cid not in chats:
                raise Problem("找不到这个对话。")
            ch = dict(chats[cid].get("chapters") or {})
        if name:
            ch[key] = name
        else:
            ch.pop(key, None)
        if cid.startswith("x:"):
            marks.setdefault(cid, {})["chapters"] = ch
            registry.write_state(self.home, marks=marks)
        else:
            chats[cid]["chapters"] = ch
            self.store.write(chats)
        return {"chapters": ch}

    # ----- 文档预览和编辑（#34） -----

    def doc_versions(self, cid, path):
        """对话里这个文件的各版本：Agent 发的、用户上传的、用户在预览里改的。不在记录里的文件不给看。"""
        if cid == devices.FS_CHAT:            # 设备管理里打开的：只有现在这一版（#100）
            if not devices.allowed_file(path):
                raise Problem("找不到这个文件。")
            return [{"by": "me", "at": os.path.getmtime(path), "snap": None}]
        out = []
        for r in self.channel.logs.read(cid):
            if r.get("type") in ("file", "edit") and r.get("path") == path:
                out.append({"by": "me" if r["type"] == "edit" else "agent", "at": r.get("at"), "snap": r.get("snap")})
            elif r.get("type") == "user":
                out += [{"by": "me", "at": r.get("at"), "snap": f["path"]} for f in r.get("files") or [] if f.get("path") == path]
        if self.ws_in(cid, path) and os.path.isfile(path):     # 工作区里的文件（#206）：现在这一版就是最新的
            if not out or out[-1].get("snap"):
                out.append({"by": "me", "at": os.path.getmtime(path), "snap": None})
        if not out:
            raise Problem("找不到这个文件。")
        return out

    def doc_view(self, cid, path, v=None):
        vs = self.doc_versions(cid, path)
        src = path
        if v not in (None, "") and int(v) < len(vs) - 1:      # 旧版本看副本；最新的看原文件
            src = vs[int(v)].get("snap")
            if not src or not os.path.isfile(src):
                raise Problem("这一版没有留底。")
        try:
            out = docs.view(src, os.path.dirname(path))
        except docs.Problem as e:
            raise Problem(str(e))
        out.update(name=os.path.basename(path), path=path, file=src, versions=[{"by": x["by"], "at": x["at"]} for x in vs],
                   v=len(vs) - 1 if src == path else int(v), latest=src == path)
        return out

    def doc_reveal(self, cid, path):
        """在访达里选中对话里的这个文件（#392）：能打开预览的就能显示，不限工作目录。"""
        self.doc_versions(cid, path)
        if not os.path.exists(path):
            raise Problem("找不到这个文件了。")
        subprocess.run(["open", "-R", path], capture_output=True)

    def doc_save(self, cid, path, text, mtime):
        self.doc_versions(cid, path)
        if not isinstance(text, str):
            raise Problem("内容不对。")
        try:
            saved = docs.save(path, text, mtime)
        except docs.Problem as e:
            raise Problem(str(e))
        self.channel.user_saved[(cid, os.path.realpath(path))] = saved
        if cid == devices.FS_CHAT:
            return self.doc_view(cid, path)
        rec = {"type": "edit", "id": "edit-" + uuid.uuid4().hex[:10], "path": path, "name": os.path.basename(path)}
        log_ = self.channel.logs.read(cid)
        last = log_[-1] if log_ else None
        if last and last.get("type") == "edit" and last.get("path") == path and last.get("id"):
            rec["id"] = last["id"]            # 连着几次保存（工作区里自动保存）算一次，不在对话里刷屏
            old = last.get("snap")
            if old and os.path.isfile(old) and os.path.dirname(old) == docs.snap_dir(self.channel.logs.folder, cid):
                try:
                    os.remove(old)
                except OSError:
                    pass
        snap = docs.snapshot(self.channel.logs.folder, cid, path)
        if snap:
            rec["snap"] = snap
        self.channel._emit(cid, self.channel.logs.append(cid, rec))
        chats = self.chats()
        if cid in chats:                       # 下一次发消息时告诉助手
            chats[cid]["doc_edits"] = sorted(set(chats[cid].get("doc_edits") or []) | {path})
            self.store.write(chats)
        return self.doc_view(cid, path)

    def _doc_edits(self, cid):
        chats = self.chats()
        paths = (chats.get(cid) or {}).pop("doc_edits", None)
        if not paths:
            return ""
        self.store.write(chats)
        return "（我在预览里直接改过这些文件，以现在的内容为准：" + "、".join(paths) + "）\n\n"

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
                try:
                    t = c.get("title") or history.title(base, sid, cwd) or f"{where}{kind}"
                except Exception:                 # 对话记录读不了不影响列表
                    t = f"{where}{kind}"
                rows.append({"id": "x:" + sid, "sid": sid, "agent": a_id, "agent_name": a["name"], "source": where,
                             "title": t, "last_active": c.get("last_active"),
                             "busy": bool(c.get("busy")), "base": base, "external": True, "dir": a.get("dir")})
            if terminal and base == "claude":
                try:
                    from claude_agent_sdk import list_sessions
                    sessions = list_sessions(directory=cwd, limit=40)
                except Exception:
                    sessions = []
                start, shown = len(rows), 0
                for x in sessions:
                    if x.session_id in mine:
                        continue
                    mine.add(x.session_id)
                    shown += 1
                    title = " ".join((x.custom_title or x.summary or x.first_prompt or "终端里的对话").split())[:30]
                    rows.append({"id": "x:" + x.session_id, "sid": x.session_id, "agent": a_id, "agent_name": a["name"],
                                 "source": "终端", "title": title, "base": base, "external": True, "busy": False,
                                 "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x.last_modified / 1000)),
                                 "dir": a.get("dir"), "_extra": shown > 15})
                self._forks(a_id, cwd, rows)
                # 只列最近 15 个，再加上它们是从哪个分出来的（整棵树）
                byid = {r["id"]: r for r in rows[start:]}
                keep = {r["id"] for r in rows[start:] if not r["_extra"]}
                for cid in list(keep):
                    seen = set()
                    while cid in byid and cid not in seen:
                        seen.add(cid)
                        keep.add(cid)
                        cid = byid[cid].get("parent")
                rows[start:] = [r for r in rows[start:] if r["id"] in keep]
                for r in rows[start:]:
                    r.pop("_extra", None)
        marks = registry.read_state(self.home).get("marks") or {}
        out = []
        seen = getattr(self, "_ext_seen", None)
        if seen is None:
            seen = self._ext_seen = {}
        now = time.time()
        for r in rows:
            mk = marks.get(r["id"]) or {}
            if mk.get("hidden"):
                continue
            r.update({k: v for k, v in mk.items() if k not in ("hidden", "chapters")})
            seen[r["id"]] = (now, dict(r))
            if mk.get("archived"):
                r["delete_at"] = delete_at({**mk, "last_active": r.get("last_active")}, archive_days())
            out.append(r)
        return out

    def _forks(self, aid, cwd, rows):
        """终端里分叉出来的对话挂到原来那个下面（原来那个可能是网页对话）。"""
        from . import history
        ids = {r["sid"]: r["id"] for r in rows if r.get("agent") == aid}
        for cid, c in self.chats().items():
            if c.get("agent") == aid and c.get("session_id") and board_mod.top_level(c, cid):
                ids.setdefault(c["session_id"], cid)
        try:
            parents = history.fork_parents(list(ids), cwd)
        except Exception:
            log.exception("认分叉出错")
            return
        for r in rows:
            p = parents.get(r.get("sid")) if r.get("agent") == aid else None
            if p and p in ids and ids[p] != r["id"]:
                r["parent"] = ids[p]

    def _external(self, cid):
        sid = cid[2:]
        hit = getattr(self, "_ext_seen", {}).get(cid)      # 列表里刚列过的直接用，不再把所有 Agent 的对话列一遍（#32）
        if hit and time.time() - hit[0] < 120:
            return dict(hit[1])
        for r in self.external_chats(terminal=True):
            if r["sid"] == sid:
                return r
        r = self._old_terminal(sid)
        if r:
            self.__dict__.setdefault("_ext_seen", {})[cid] = (time.time(), dict(r))
            return r
        raise Problem("找不到这个对话。")

    def _old_terminal(self, sid):
        """列表只列最近 15 个终端对话，更早的用链接打开时到各 Agent 的会话记录里找（#367）。"""
        if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", sid or ""):
            return None
        if any(sid in (c.get("session_id"), c.get("codex_session_id")) for c in self.chats().values()):
            return None                       # 网页里接着聊过的，列表里列的是那个网页对话
        mk = (registry.read_state(self.home).get("marks") or {}).get("x:" + sid) or {}
        if mk.get("hidden"):
            return None
        from claude_agent_sdk import get_session_info
        for a_id, a in agents.all_agents().items():
            if (a.get("base") or "claude") != "claude":
                continue
            try:
                x = get_session_info(sid, directory=agents.cwd(dict(a, id=a_id)))
            except Exception:
                x = None
            if not x:
                continue
            title = " ".join((x.custom_title or x.summary or x.first_prompt or "终端里的对话").split())[:30]
            r = {"id": "x:" + sid, "sid": sid, "agent": a_id, "agent_name": a["name"], "source": "终端",
                 "title": title, "base": "claude", "external": True, "busy": False,
                 "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x.last_modified / 1000)),
                 "dir": a.get("dir")}
            r.update({k: v for k, v in mk.items() if k not in ("hidden", "chapters")})
            return r
        return None

    def external_detail(self, cid):
        from . import history
        r = self._external(cid)
        a = agents.get(r["agent"])
        ch = ((registry.read_state(self.home).get("marks") or {}).get(cid) or {}).get("chapters") or {}
        term = r["source"] == "终端" and r["base"] == "claude"
        return dict(r, log=history.read(r["base"], r["sid"], agents.cwd(a)), progress=None, readonly=True,
                    model=None, effort=None, temp=False, chapters=ch, linkable=term,
                    open_elsewhere=history.open_elsewhere(r["sid"]) if term else None,
                    sig=history.sig(r["base"], r["sid"], agents.cwd(a)))

    def chat_sig(self, cid):
        """网页停在终端、桌面端的对话上时隔几秒问一次：记录变了就重新取，桌面端开没开着也跟着变。"""
        from . import history
        if cid.startswith("x:"):
            r = self._external(cid)
            a = agents.get(r["agent"])
            sid, base = r["sid"], r["base"]
        else:
            m = self.meta(cid)
            if m.get("away") and not m.get("origin"):     # 转到微信、飞书接着聊过（#212）
                a = agents.get(m["agent"]) if m.get("agent") else None
                base = self._effective(m)["base"]
                sid = m.get("codex_session_id" if base == "codex" else "session_id")
                return {"sig": history.sig(base, sid, agents.cwd(a) if a else None) if sid else None, "open_elsewhere": None}
            if not self._linked(m):
                return {"sig": None}
            a = agents.get(m["agent"]) if m.get("agent") else None
            sid, base = m["origin"], self._effective(m)["base"]
        return {"sig": history.sig(base, sid, agents.cwd(a) if a else None),
                "open_elsewhere": history.open_elsewhere(sid) if base == "claude" else None}

    def continue_chat(self, cid, text, files):
        """在网页里接着别处的对话聊：另开一个网页对话。终端、桌面端里的接着原来的会话聊，内容回得去；
        飞书、微信里的从那个对话分支出来，原来那边不受影响。"""
        check_message(self, text, files)
        r = self._external(cid)
        if r["source"] == "终端" and r["base"] == "claude":     # 终端、桌面端里的：接着原来的会话聊（#94）
            new = self.call(self.new_chat, agent=r["agent"], link=r["sid"], title=r["title"], source=r["source"])
        else:                                 # 飞书、微信那边的进程一直连着会话，两边一起写会乱：分支
            new = self.call(self.new_chat, agent=r["agent"], fork_from=r["sid"], title=r["title"], source=r["source"])
        send(self, new, text, files)
        return {"id": new}

    @staticmethod
    def _locate(rows, index, at, kind):
        """网页说的第 index 条：带了 at 就按 at 找那条 kind 记录，下标只当参考（网页开久了下标可能和这边对不上，#288）。"""
        ok = isinstance(index, int) and 0 <= index < len(rows) and rows[index].get("type") == kind
        if not isinstance(at, (int, float)) or ok and abs((rows[index].get("at") or 0) - at) < 1e-3:
            return index
        hit = [i for i, r in enumerate(rows) if r.get("type") == kind and abs((r.get("at") or 0) - at) < 1e-3]
        return min(hit, key=lambda i: abs(i - index) if isinstance(index, int) else 0) if hit else index

    def fork_chat(self, cid, index, src="log", at=None):
        """从这个对话的第 index 条回答分出一个新对话：之前的内容带过去，之后两边各聊各的。
        src=earlier：从接着聊之前（终端、桌面端里）的回答分；终端、桌面端的对话（x:）直接按记录里的编号分。"""
        if cid.startswith("x:") or src == "earlier":
            return self._fork_history(cid, index)
        m = self.meta(cid)
        if m.get("busy"):
            raise Problem("等它这一轮做完再分叉。")
        rows = self.channel.logs.read(cid)
        index = self._locate(rows, index, at, "answer")
        if not isinstance(index, int) or not 0 <= index < len(rows) or rows[index].get("type") != "answer":
            raise Problem("找不到这条回答。")
        base = self._effective(m)["base"]
        last = max(i for i, r in enumerate(rows) if r.get("type") == "answer")
        fork = rows[index].get("fork") or {}
        sid = m.get("codex_session_id" if base == "codex" else "session_id")
        if index == last and sid:
            at = None if base == "codex" else fork.get("at")
        elif fork.get("at") and base != "codex":
            sid, at = fork.get("sid") or sid, fork["at"]
        else:
            raise Problem("这条回答之后的内容分不开，只能从最后一条回答分叉。")
        if not sid:
            raise Problem("这个对话还没开始，没法分叉。")
        t = m.get("temp")
        # 标题留空：分出来以后的第一句话当标题
        new = self.new_chat(agent=m.get("agent"), temp=dict(t) if t else None, fork_from=sid, fork_at=at, parent=cid)
        if t and not t.get("dir"):            # 没选目录的临时对话：接着用原来那个临时目录（会话记录按目录存）
            chats = self.chats()
            chats[new]["temp"]["scratch"] = t.get("scratch") or cid
            self.store.write(chats)
        for r in rows[:index + 1]:
            if r.get("type") in ("user", "answer", "file", "chatlog", "note", "mention"):
                self.channel.logs.append(new, r)      # 带着 fork：在新对话里也能从之前的回答再分
        return {"id": new}

    def handoff_prep(self, cid, kind):
        """转到微信 / 飞书接着聊（#212）之前：找到 Agent 接的那个渠道，算出要接上的会话和位置。"""
        m = self.meta(cid)
        a = agents.get(m.get("agent")) if m.get("agent") else None
        if not a or m.get("mention_of") or m.get("side_of") or share.chat_owner(self, cid):
            raise Problem("这个对话转不过去。")
        if m.get("cwd") and m["cwd"] != agents.cwd(a):
            raise Problem("这个对话转不过去。")
        hit = [(p, path) for k, p, path in agents.linked_paths(a["id"]) if k == kind]
        if not hit:
            return {"need_connect": True}
        proc, path = hit[0]
        base = self._effective(m)["base"]
        rows = self.channel.logs.read(cid)
        if m.get("busy"):                     # 不分支、接着同一个会话聊，所以要等这一轮做完
            raise Problem("等它这一轮做完再转。")
        sid = m.get("codex_session_id" if base == "codex" else "session_id")
        last = next((r for r in reversed(rows) if r.get("type") == "answer"), None)
        if not sid or not last:
            raise Problem("还没有回答，没什么可接着聊的。")
        req = {"session_id": sid, "link": True, "title": m.get("title") or "网页里的对话", "last_reply": last.get("text") or "",
               "model": m.get("model"), "effort": m.get("effort"), "project": path}
        return {"proc": proc, "req": req}

    def handoff_note(self, cid, label):
        """转过去以后：网页对话里加一条分隔、标成章节；之后那边说的从会话记录里读出来穿插显示（chat_detail 的 elsewhere）。"""
        rec = self.channel.logs.append(cid, {"type": "note", "divider": True, "text": f"以下是在{label}里接着聊的"})
        chats = self.chats()
        if cid in chats:
            m = chats[cid]
            m["away"] = (m.get("away") or [])[-9:] + [{"to": label, "at": rec.get("at") or time.time()}]
            if rec.get("at"):
                m["chapters"] = dict(m.get("chapters") or {}, **{f"note:{rec['at']}": f"转到{label}"})
            self.store.write(chats)
        self.channel._emit(cid, rec)

    def _back_from_away(self, cid, m):
        """转到微信、飞书以后，第一次回网页里说话：加一条分隔。"""
        away = m.get("away") or []
        if not away or away[-1].get("back"):
            return
        chats = self.chats()
        if cid in chats:
            chats[cid]["away"][-1]["back"] = time.time()
            self.store.write(chats)
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "note", "divider": True, "text": "以下是回到网页接着聊的"}))

    def _fork_history(self, cid, index):
        d = self.chat_detail(cid)
        rows = d["log"] if cid.startswith("x:") else d.get("earlier") or []
        if d.get("busy"):
            raise Problem("等它这一轮做完再分叉。")
        if not isinstance(index, int) or not 0 <= index < len(rows) or rows[index].get("type") != "answer":
            raise Problem("找不到这条回答。")
        if d["base"] == "codex" or not rows[index].get("uuid"):
            raise Problem("这条回答分不了叉。")
        sid = d["sid"] if cid.startswith("x:") else self.meta(cid)["origin"]
        new = self.new_chat(agent=d.get("agent"), fork_from=sid, fork_at=rows[index]["uuid"], parent=cid)
        for r in rows[:index + 1]:
            self.channel.logs.append(new, {k: v for k, v in r.items() if k not in ("uuid", "elsewhere")})
        return {"id": new}

    # ----- 退回（#24） -----

    def _rewind_plan(self, rows, i, base, m=None):
        """退回到第 i 条（自己发的消息）之前：返回 (文件恢复到哪条消息, 接到哪个会话, 接到哪条)；退不了返回 None。"""
        if base == "codex" or not 0 <= i < len(rows) or rows[i].get("type") != "user" or rows[i].get("by"):
            return None
        prev = [r for r in rows[:i] if r.get("type") == "answer"]
        sid = at = None
        if not prev and m and m.get("continued_at") and m.get("origin"):
            # 接着终端、桌面端的对话聊的第一条：接回转过来之前那边的最后一条回答
            from . import history
            a = agents.get(m["agent"]) if m.get("agent") else None
            before = [r for r in history.read(base, m["origin"], agents.cwd(a) if a else None)
                      if r["type"] == "answer" and (r.get("at") or 0) < m["continued_at"]]
            if not before or not before[-1].get("uuid"):
                return None
            sid, at = m["origin"], before[-1]["uuid"]
        elif prev:
            f = prev[-1].get("fork") or {}
            if not f.get("at"):
                return None
            sid, at = f.get("sid"), f["at"]
        nxt = next((r for r in rows[i + 1:] if r.get("type") == "answer"), None)
        return (nxt or {}).get("prompt"), sid, at

    def rewindable(self, cid, rows):
        m = self.meta(cid)
        base = self._effective(m)["base"]
        out = {}
        for i, r in enumerate(rows):
            if r.get("type") == "user":
                p = self._rewind_plan(rows, i, base, m)
                if p:
                    out[i] = {"files": bool(p[0]), "turns": sum(1 for x in rows[i:] if x.get("type") == "user")}
        return out

    async def rewind_preview(self, cid, index, at=None):
        """退回第 index 条时会恢复哪些文件（相对工作目录）。"""
        m = self.meta(cid)
        rows = self.channel.logs.read(cid)
        index = self._locate(rows, index, at, "user")
        plan = self._rewind_plan(rows, index if isinstance(index, int) else -1, self._effective(m)["base"], m)
        if not plan or not plan[0] or m.get("busy"):
            return {"files": []}
        c = self._bridge(cid).chat(cid, "p2p")
        try:
            files = await c.rewind(plan[0], None, None, dry=True)
        except Exception:
            log.warning("算不出会恢复哪些文件", exc_info=True)
            return {"files": []}
        base = os.path.realpath(self._bridge(cid).path)
        return {"files": [os.path.relpath(f, base) if os.path.realpath(f).startswith(base + os.sep) else f for f in files]}

    async def rewind_chat(self, cid, index, files, at=None):
        m = self.meta(cid)
        if m.get("busy"):
            raise Problem("它还在回答，先停下再退回。")
        rows = self.channel.logs.read(cid)
        index = self._locate(rows, index, at, "user")
        plan = self._rewind_plan(rows, index if isinstance(index, int) else -1, self._effective(m)["base"], m)
        if not plan:
            raise Problem("这条消息退不回去。")
        files_at, sid, at = plan
        c = self._bridge(cid).chat(cid, "p2p")
        try:
            await c.rewind(files_at if files else None, sid, at)
        except RuntimeError as e:
            raise Problem(str(e))
        except Exception as e:
            raise Problem(f"退回失败：{e}")
        self.channel.logs.truncate(cid, index)
        self.channel.progress_of.pop(cid, None)
        r = rows[index]
        return {"text": r.get("text") or "", "files": r.get("files") or []}

    async def peer_task(self, cid, aid, prompt, title, project, kind=None, of=None, shown=None, depth=0, side=False):
        """别的电脑上的项目、群聊交给这台电脑的 Agent：在这里开（或接着）一个对话做，马上回一个编号，那边再来问结果。"""
        from .session import Task
        if not agents.get(aid):
            raise Problem("这台电脑上没有这个 Agent。")
        if not re.fullmatch(r"[pm]-[\w-]+", cid or ""):
            raise Problem("对话编号不对。")
        self.store.deleted.discard(cid)
        chats = self.chats()
        if cid not in chats:
            chats[cid] = {"type": "p2p", "agent": aid, "title": title, "remote_project": True,
                          "created": registry.now(), "last_active": registry.now(),
                          **({"mention_of": of} if kind == "mention" else {"project": project}),
                          **({"side_at": True} if side and kind == "mention" else {})}   # 侧边对话里被 @ 的（#290）：只读
            self.store.write(chats)
        start = time.time()
        b = self._bridge(cid)
        chat = b.chat(cid, "p2p")
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "text": shown or prompt, "files": [],
                                                                **({"from_chat": of} if kind == "mention" else {})}))
        task = Task(prompt)
        task.depth = int(depth or 0)
        chat.submit(task)
        token = uuid.uuid4().hex
        self.peer_results[token] = None       # 活可能做很久：先回个编号，那边隔一会儿来问结果
        self.peer_runs[token] = (cid, start)

        async def wait():
            try:
                state, answer = await task.future
            except Exception as e:
                state, answer = "error", str(e)
            state = {"ok": "done"}.get(state, state)
            last = next((r for r in reversed(self.channel.logs.read(cid))
                         if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
            self.peer_results[token] = {"done": True, "state": state, "text": answer or last.get("text") or "",
                                        "steps": last.get("steps") or [], "said": last.get("said"),
                                        "elapsed": last.get("elapsed"), "ctx": (self.chats().get(cid) or {}).get("ctx")}
        t = asyncio.ensure_future(wait())
        self.background.add(t)
        t.add_done_callback(self.background.discard)
        return {"token": token}

    def peer_result(self, token):
        if token not in self.peer_results:
            raise Problem("unknown")
        r = self.peer_results[token]
        cid, start = self.peer_runs.get(token) or (None, 0)
        if r is not None:
            self.peer_results.pop(token, None)
            self.peer_runs.pop(token, None)
        # 被 @ 的 Agent 打开了这台电脑上的应用：交给那边，在用户看着的对话里打开（#337）
        apps = [{"app": x.get("app"), "name": x.get("name"), "icon": x.get("icon") or "", "at": x.get("at")}
                for x in (self.channel.logs.read(cid) if cid else []) if x.get("type") == "app" and x.get("at", 0) >= start]
        return dict(r or {"done": False}, apps=apps)

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *args, **kw):
        """在主循环里执行（state.json 只在主循环里改，不和 Chat 的保存抢）。"""
        async def go():
            return fn(*args, **kw)
        return self.run(go())

    def bg(self, coro):
        """不等结果的（发消息：回答通过 SSE 推过去）。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: f.exception() and log.error("处理出错", exc_info=f.exception()))

    def ask(self, req):
        pass

    async def shutdown(self):
        if getattr(self, "peers", None):
            self.peers.stop()
        for b in list(self.bridges.values()):
            await b.shutdown()

    # ----- HTTP -----

    def serve(self):
        port = self.entry.get("port") or PORT
        server = None
        for p in range(port, port + 20):
            try:
                server = Server(("127.0.0.1", p), _handler(self))
                break
            except OSError:
                continue
        if not server:
            raise RuntimeError(f"端口 {port}～{port + 19} 都被占用了")
        server.daemon_threads = True
        registry.write_state(self.home, port=server.server_address[1])
        try:
            apps_mod.ensure()             # Agent 做应用前要读这里的 README（#103）
        except OSError:
            log.exception("应用目录建不了")
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        log.info("网页：http://127.0.0.1:%d/", server.server_address[1])
        return server


def _cursor(t):
    """翻页的位置：最后活跃 + 编号（同一秒的也分得开）。"""
    return f"{t[0]}|{t[1]}"


def _uncursor(c):
    at, _, cid = str(c).partition("|")
    return at, cid


def _defaults(base, path):
    """不传参数时 Claude Code / Codex 自己会用的模型和思考深度（只用于显示）。"""
    if base == "codex":
        from .codex import defaults
        return defaults(path)
    m, e, _ = registry.claude_defaults(path)
    return m, e


def _snapshot(fn):
    with registry.snapshot():
        return fn()


# ---------- HTTP 接口 ----------

def _handler(pf):
    from . import web as console
    from . import projects as projects_mod
    from . import groups as groups_mod
    from . import schedules as schedules_mod

    class Handler(BaseHTTPRequestHandler):
        server_version = "ccim"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self._st, self._sz = code, len(data)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if getattr(self, "_pt", None):
                self.send_header("Server-Timing", f"app;dur={self._pt.took():.1f}")   # 网页拿它分出网络和后台各花了多久
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guest(self, method, path, q, body):
            """共享电脑上访客的请求（#154）：不让做的回 403，返回访客的账号编号（主人返回 None）。"""
            acct = share.account_of(self.headers)
            skills_mod.SPACE.set(acct)            # 访客装、改的 Skill 在它自己那一份里（#154）
            if not acct or not path.startswith("/api/") or path == "/api/events":
                return None, False
            from urllib.parse import unquote
            name = unquote(self.headers.get("X-Account-Name") or "")
            try:
                share.remember(pf, acct, name, share.share_of(self.headers))
                share.check(pf, acct, method, path, q, body)
            except share.Denied as e:
                self._send(403, {"error": str(e)})
                return acct, True
            return acct, False

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
            except (Problem, console.Problem, projects_mod.Problem, schedules_mod.Problem, devices.Problem, skills_mod.Problem, ValueError) as e:
                self._send(400, {"error": str(e)})
            except SystemExit as e:
                self._send(400, {"error": str(e.code)})
            except Exception as e:
                log.exception("接口出错")
                self._send(500, {"error": f"出错了：{e}"})

        def _remote(self, body=None):
            """网页要操作另一台电脑上的东西（X-Machine）：交给那台电脑执行。返回 True 表示已处理。"""
            mid = self.headers.get("X-Machine") or \
                (parse_qs(urlparse(self.path).query).get("machine") or [""])[0]   # 图片等 <img> 带不了头，用参数
            if not mid or mid == pf.peers.me["id"]:
                return False
            path = urlparse(self.path).path
            if path == "/api/file" and self.command == "GET":   # 链接、<img>、<video> 带不了 X-CCIM 头
                if not self._host_ok() or not self._fs_ok():
                    self._send(403, b"forbidden", "text/plain")
                else:
                    filesend.serve_peer(self, pf.peers, mid)
                return True
            if not self._guard():
                return True
            if body is None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
            from .peers import Offline
            try:
                status, ctype, data = pf.peers.forward(mid, self.command, self.path, body,
                                                       {k: self.headers[k] for k in ("X-Name", "Content-Type") if self.headers.get(k)})
            except Offline:
                name = next((m["name"] for m in pf.peers.machines() if m["id"] == mid), "那台电脑")
                if self.command == "POST" and path in ("/api/chat/send", "/api/chat/mention"):
                    pf.peers.queue_out(mid, "POST", self.path, body)
                    self._send(200, {"queued": True, "machine": name})
                else:
                    self._send(503, {"error": f"「{name}」现在离线，等它连上再试。"})
                return True
            except Exception as e:
                log.exception("转交出错")
                self._send(502, {"error": f"出错了：{e}"})
                return True
            self._send(status, data, ctype)
            return True

        def _timed(self, fn):
            """接口耗时记进性能数据（#286）：经总控转来的、要转给别的电脑的都记上来处、去处。"""
            u = urlparse(self.path)
            self._st = self._sz = None
            if not u.path.startswith("/api/") or u.path in ("/api/events", "/api/perf"):
                self._pt = None
                return fn()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            to = self.headers.get("X-Machine") or q.get("machine")
            with perf.Track("api", f"{self.command} {u.path}", via="peer" if self.headers.get("X-From-Peer") else None,
                            to=to if to and to != pf.peers.me["id"] else None, **perf.ctx_of(q)) as t:
                self._pt = t
                try:
                    return fn()
                finally:
                    t.status, t.size = getattr(self, "_st", None), getattr(self, "_sz", None)

        def do_GET(self):
            self._timed(self._get)

        def _get(self):
            filesend.unname(self)
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path.startswith("/api/") and u.path != "/api/events" and self._remote():
                return
            acct, denied = self._guest("GET", u.path, q, None)
            if denied:
                return
            if u.path in ("/", "/index.html"):
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path in STATIC:
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                data, ctype = static_file(u.path)
                return self._send(200, data, ctype)
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/file":
                return self._file(q.get("chat", ""), q.get("path", ""))
            if u.path == "/api/file/part":
                return self._file_part(q)
            routes = {
                "/api/state": lambda: pf.shared.get(("state", bool(q.get("local")), acct), lambda: state(pf, acct) if q.get("local")
                                                    else pf.peers.merged_state(state(pf))),
                "/api/dirs": lambda: list_dirs(q.get("path") or "", share.root(acct) if acct else None),
                "/api/kb": lambda: {"libs": share.filter_libs(kb_mod.libs(), acct)},
                "/api/kb/lib": lambda: kb_mod.lib_detail(q.get("id", "")),
                "/api/kb/entry": lambda: kb_mod.entry(q.get("lib", ""), q.get("id", "")),
                "/api/kb/search": lambda: {"results": [r for r in kb_mod.search(q.get("q", ""))
                                                       if r["lib"] in {x["id"] for x in share.filter_libs(kb_mod.libs(), acct)}]},
                "/api/kb/manifest": lambda: kb_mod.manifest(),
                "/api/skills": lambda: {"skills": skills_mod.view_listing()},
                "/api/skills/detail": lambda: skills_mod.detail(q.get("name", "")),
                "/api/skills/local": lambda: {"skills": skills_mod.local_list()},
                "/api/skills/file": lambda: skills_mod.read_file(q.get("name", ""), q.get("path", "")),
                "/api/skills/market": lambda: skills_mod.market(q.get("name", ""), q.get("repo") or None),
                "/api/agent/skills": lambda: {"groups": skills_mod.agent_skills(_agent(q.get("id")))},
                "/api/agent/skill": lambda: skills_mod.agent_detail(_agent(q.get("id")), q.get("group", ""), q.get("name", "")),
                "/api/agent/skill/file": lambda: skills_mod.agent_read(_agent(q.get("id")), q.get("group", ""), q.get("name", ""), q.get("path", "")),
                "/api/skills/manifest": lambda: skills_mod.manifest(),
                "/api/chats": lambda: pf.chat_page(acct, q.get("before") or None, q.get("archived") == "1", (q.get("q") or "").strip(),
                                                   q.get("groups") == "1"),
                # 只读：在请求线程里算，不占主循环（#335）；一个个对话取视图要反复读 state.json，快照里只读一遍
                "/api/board": lambda: _snapshot(lambda: board_mod.stats(pf, lambda cid: share.owner_map(pf).get(cid) == acct, acct is None, acct)),
                "/api/board/day": lambda: _snapshot(lambda: board_mod.day(pf, q.get("d", ""), lambda cid: share.owner_map(pf).get(cid) == acct, acct is None, acct)),
                # 复盘的 Agent 在别的电脑上时，从这台拉这几天的材料（#364）
                "/api/board/material": lambda: _snapshot(lambda: {"material": board_mod.material(
                    pf, *board_mod.check_range(q.get("from", ""), q.get("to", "")), lambda cid: share.owner_map(pf).get(cid) == acct, local=False, acct=acct)}),
                "/api/board/review_views": lambda: {"views": board_mod.views(registry.global_config())},
                "/api/dev": lambda: pf._dev().overview(fresh=bool(q.get("fresh"))),
                "/api/chat": lambda: pf.chat_detail(q.get("id", "")),
                "/api/chat/sig": lambda: pf.chat_sig(q.get("id", "")),
                "/api/doc": lambda: pf.doc_view(q.get("chat", ""), q.get("path", ""), q.get("v")),
                "/api/apps": lambda: {"apps": apps_mod.listing()},
                "/api/app": lambda: apps_mod.page(q.get("id", "")),
                "/api/app/sig": lambda: {"sig": apps_mod.data_sig(q.get("id", "")), "psig": apps_mod.page_sig(q.get("id", ""))},
                "/api/app/query": lambda: {"rows": apps_mod.query(q.get("id", ""), q.get("coll", ""), q.get("q", ""))} if not q.get("rid")
                    else {"row": apps_mod.query(q.get("id", ""), q.get("coll", ""), rid=q["rid"])},
                "/api/app/list": lambda: apps_mod.data_list(q.get("id", ""), q.get("dir", "")),
                "/api/app/read": lambda: apps_mod.data_read(q.get("id", ""), q.get("path", "")),
                "/api/ws/ls": lambda: pf.ws_call(q.get("chat", ""), workspace.ls, q.get("path", ""), q.get("hidden") == "1"),
                "/api/agent": lambda: share.filter_agent_chats(pf, agent_view(pf, q.get("id", ""), detail=True), acct),
                "/api/agent/recent": lambda: agent_recent(pf, q.get("id", ""), acct),
                "/api/project": lambda: pf.call(pf.projects.detail, q.get("id", "")),
                "/api/project/diff": lambda: pf.projects.diff(q.get("id", ""), q.get("task", ""), q.get("agent", "")),
                "/api/schedule": lambda: pf.call(pf.schedules.detail, q.get("id", "")),
                "/api/schedule/list": lambda: {"schedules": [x for x in pf.call(pf.schedules.summary)
                                                             if share.of_schedule(pf, x) == acct]},   # ccim schedule list
                "/api/logs": lambda: {"text": console.logs(q.get("proc", ""))},
                "/api/recent": lambda: {"folders": console.recent_folders()},
                "/api/job": lambda: (console.JOBS[q["id"]].view() if q.get("id") in console.JOBS else console._missing()),
                "/api/fs/info": lambda: devices.overview(registry.pdir(pf.home)),
                "/api/fs/ls": lambda: devices.ls(q.get("path") or "~", q.get("hidden") == "1"),
                "/api/fs/scan": lambda: pf.cleaner.view(),
                "/api/fs/perms": lambda: {"perms": devices.perms(fresh=True)},
                "/api/slash": lambda: {"skills": slash_skills(q, acct)},          # 输入 / 时列出的 Skill（#381）
                "/api/usage": lambda: {"text": pf.run(usage_report(q.get("dir") or ""), timeout=90)},   # 新对话页的 /usage
                "/api/perf": lambda: perf.summary(q.get("hours") or 24),
                "/api/diary/home": lambda: pf.diary_home_view(),         # 日记存在哪台电脑（#376）
                "/api/diary/info": lambda: {"path": diary_mod.root(), "apps": diary_mod.apps()},   # 这台上的日记文件夹、能用什么打开
            }
            if u.path in ("/api/state", "/api/agent", "/api/agent/recent", "/api/chats"):     # 只读、又要把各个文件查很多遍的：一次请求里只读一遍
                fn = routes[u.path]
                return self._api(lambda: _snapshot(fn))
            if u.path in routes:
                return self._api(routes[u.path])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                self._timed(self._post)
            finally:
                pf.shared.changed()           # 做完一次改动：之后来要整体状态的都重新算（#258）

        def _post(self):
            u = urlparse(self.path)
            if self._remote():
                return
            # 别的电脑转来的：/api/ws/reveal、/api/doc/reveal 只给这台电脑的主人（#322），访客不行
            if self.headers.get("X-From-Peer") and (u.path in ("/api/pick", "/api/reveal")
                                                     or u.path in ("/api/ws/reveal", "/api/doc/reveal") and share.account_of(self.headers)):
                return self._send(403, {"error": "只能在这台电脑上用"})
            if u.path == "/api/upload":
                if self._guest("POST", u.path, {}, None)[1]:
                    return
                return self._api(self._upload)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if getattr(self, "_pt", None):
                self._pt.ctx.update(perf.ctx_of(body=body))
            acct, denied = self._guest("POST", u.path, {}, body)
            if denied:
                return
            b = body.get
            routes = {
                "/api/chat/new": lambda: {"id": pf.call(pf.new_chat, agent=b("agent"), temp=b("temp"), account=acct)},
                "/api/chat/send": lambda: send(pf, b("id"), b("text") or "", b("files") or [], b("ctx"), b("refs"), acct),
                "/api/helper": lambda: {"id": pf.run(pf.helper_chat(b("kind"), bool(b("fresh")), acct))},
                "/api/pocket": lambda: {"id": pf.call(pf.pocket_chat, acct, bool(b("fresh")), b("base"))},
                "/api/pocket/settings": lambda: set_pocket(body),
                "/api/chat/side": lambda: {"id": pf.run(pf.side_chat(b("id"), bool(b("fresh"))))},
                "/api/chat/continue": lambda: pf.continue_chat(b("id"), b("text") or "", b("files") or []),
                "/api/chat/stop": lambda: {"stopped": pf.run(pf.stop_chat(b("id")))},
                "/api/chat/skip": lambda: {"skipped": pf.run(pf.skip_step(b("id")))},
                "/api/chat/action": lambda: pf.run(pf.action(b("id"), b("mid"), b("value"))),
                "/api/chat/set": lambda: pf.run(pf.set_chat(b("id"), b("key"), b("value"))),
                "/api/chat/delete": lambda: pf.call(pf.delete_terminal, b("id")) if str(b("id")).startswith("x:")
                    else pf.run(pf.delete_chat(b("id"))),
                "/api/chat/mark": lambda: pf.call(pf.mark_chat, b("id"), b("changes")),
                "/api/kb/lib/save": lambda: kb_write(pf, kb_mod.save_lib, b("id"), b("name"), b("desc") or "", b("color"), acct),
                "/api/kb/lib/delete": lambda: kb_write(pf, kb_mod.delete_lib, b("id")),
                "/api/kb/entry/save": lambda: kb_write(pf, kb_mod.save_entry, b("lib"), b("id"), b("title"), b("content"), b("source")),
                "/api/kb/entry/delete": lambda: kb_write(pf, kb_mod.delete_entry, b("lib"), b("id")),
                "/api/kb/entry/move": lambda: kb_write(pf, kb_mod.move_entry, b("lib"), b("id"), b("to")),
                "/api/kb/export": lambda: kb_mod.export(b("keys")),
                "/api/kb/sync_now": lambda: (pf.kb_wake.set(), {})[1],
                "/api/skills/install": lambda: skill_write(pf, install_skill, pf, body),
                "/api/skills/toggle": lambda: skill_write(pf, skills_mod.set_disabled, b("name"), not b("on")),
                "/api/skills/delete": lambda: skill_write(pf, skills_mod.delete, b("name")),
                "/api/skills/update": lambda: skill_write(pf, skills_mod.update, b("name"), pf.peers.me),
                "/api/skills/file/save": lambda: skill_write(pf, skills_mod.save_file, b("name"), b("path"), b("text"), b("mtime")),
                "/api/agent/skill/file/save": lambda: skills_mod.agent_save(_agent(b("id")), b("group"), b("name"), b("path"), b("text"), b("mtime")),
                "/api/agent/skill/share": lambda: skill_write(pf, lambda: {"names": skills_mod.agent_share(_agent(b("id")), b("group"), b("name"),
                                                                                                           pf.peers.me.get("name") or "")}),
                "/api/skills/export": lambda: skills_mod.export(b("keys")),
                "/api/chat/seen": lambda: pf.call(pf.mark_seen, b("id")),
                "/api/chat/dismiss": lambda: pf.call(pf.dismiss, b("id")),
                "/api/chat/kbs": lambda: pf.call(pf.set_kbs, b("id"), b("kbs")),
                "/api/upgrade": lambda: start_upgrade(bool(b("force"))),
                "/api/archive_days": lambda: set_archive_days(str(b("value"))),
                "/api/workspace": lambda: set_workspace(bool(b("on"))),
                "/api/diary_cards": lambda: set_diary_cards(bool(b("on"))),
                "/api/diary_style": lambda: set_diary_style(str(b("value") or "")),
                # 日记存在哪台电脑（#376）：add / query 是飞书、微信的进程和别的电脑转来的（here），open 在这台上打开日记文件夹
                "/api/diary/add": lambda: pf.diary_write(b("text") or "", b("where") or "", b("at"), b("link"), bool(b("here"))),
                "/api/diary/query": lambda: pf.diary_query(b("op"), body, bool(b("here"))),
                "/api/diary/home": lambda: {"machine": pf.peers.set_diary_home(str(b("machine") or ""))},
                "/api/diary/open": lambda: diary_mod.open_in(b("app")),
                "/api/board/review_views": lambda: (registry.set_global(review_views=board_mod.clean_views(b("views")), review_prompt=None),
                                                    {"views": board_mod.views(registry.global_config())})[1],
                "/api/doc/reveal": lambda: pf.call(pf.doc_reveal, b("chat"), b("path")),
                "/api/doc/save": lambda: pf.call(pf.doc_save, b("chat"), b("path"), b("text"), b("mtime")),
                "/api/app/write": lambda: apps_mod.data_write(b("id"), b("path"), b("text")),
                "/api/app/remove": lambda: apps_mod.data_remove(b("id"), b("path")),
                "/api/app/save": lambda: {"row": apps_mod.save(b("id"), b("coll"), b("rec")), "sig": apps_mod.data_sig(b("id"))},
                "/api/app/del": lambda: {**apps_mod.delete(b("id"), b("coll"), b("rid")), "sig": apps_mod.data_sig(b("id"))},
                "/api/app/rename": lambda: apps_mod.rename(b("id"), b("name")),
                "/api/ws/paths": lambda: pf.ws_call(b("chat"), workspace.paths, b("paths") or []),
                "/api/ws/reveal": lambda: pf.ws_call(b("chat"), workspace.reveal, b("path") or ""),
                "/api/ws/new": lambda: pf.ws_call(b("chat"), workspace.create, b("path") or "", b("name"), bool(b("dir"))),
                "/api/ws/rename": lambda: pf.ws_call(b("chat"), workspace.rename, b("path") or "", b("name")),
                "/api/ws/move": lambda: pf.ws_call(b("chat"), workspace.move, b("path") or "", b("to") or ""),
                "/api/ws/trash": lambda: pf.ws_call(b("chat"), workspace.trash, b("path") or ""),
                "/api/ws/restore": lambda: pf.ws_call(b("chat"), workspace.restore, b("token"), b("path") or ""),
                "/api/ws/put": lambda: pf.ws_call(b("chat"), workspace.put, os.path.join(registry.pdir(pf.home), "uploads"),
                                                  b("key"), b("path") or "", b("name")),
                "/api/chat/chapter": lambda: pf.call(pf.set_chapter, b("id"), b("key"), b("name")),
                "/api/chat/fork": lambda: pf.call(pf.fork_chat, b("id"), b("index"), b("src") or "log", b("at")),
                "/api/chat/rewind_preview": lambda: pf.run(pf.rewind_preview(b("id"), b("index"), b("at")), timeout=90),
                "/api/chat/rewind": lambda: pf.run(pf.rewind_chat(b("id"), b("index"), bool(b("files")), b("at")), timeout=120),
                "/api/peer/task": lambda: pf.run(pf.peer_task(b("chat"), b("agent"), b("prompt") or "", b("title") or "项目",
                                                              b("project") or "", b("kind"), b("of"), b("shown"), b("depth"),
                                                              bool(b("side")))),
                "/api/chat/members": lambda: set_members(pf, b("id"), b("add") or [], b("remove") or []),
                "/api/chat/note": lambda: pf.call(groups_mod.set_note, pf, b("id"), b("text") or ""),
                "/api/peer/result": lambda: pf.call(pf.peer_result, b("token") or ""),
                "/api/agent/pin": lambda: (agents.update(b("id"), pinned=bool(b("on"))), {})[1],
                "/api/chat/mention": lambda: mention(pf, b("id"), b("text") or "", b("to") or [], b("files") or [], acct),
                "/api/chat/mention/stop": lambda: {"stopped": pf.run(pf.mentions.stop(b("rid") or ""))},
                "/api/chat/secret": lambda: answer_secret(pf, b("id"), b("password"), b("run"), bool(self.headers.get("X-From-Peer"))),
                "/api/peers/code": lambda: pf.peers.new_code(),
                "/api/peers/remove": lambda: pf.peers.remove(b("id")),
                "/api/peers/leave": lambda: leave_hub(pf),
                "/api/agent/save": lambda: save_agent(pf, b("id"), b("fields") or {}, acct),
                "/api/project/save": lambda: pf.call(pf.projects.save, b("id"), b("name"), b("dir"), b("members")),
                "/api/project/delete": lambda: pf.run(pf.projects.delete(b("id")), 120),
                "/api/project/member": lambda: pf.call(pf.projects.member, b("id"), b("add"), b("remove")),
                "/api/project/dispatch": lambda: pf.run(pf.projects.dispatch(b("id"), b("text"), b("to"), b("compare"), b("quote")), 600),
                "/api/project/choose": lambda: pf.projects.choose(b("id"), b("task"), b("agent")),
                "/api/project/stop": lambda: pf.run(pf.projects.stop(b("id"), b("task"), b("agent"))),
                "/api/schedule/save": lambda: pf.call(pf.schedules.save, b("id"), b("fields") or {}),
                "/api/schedule/toggle": lambda: pf.call(pf.schedules.toggle, b("id"), b("on")),
                "/api/schedule/delete": lambda: pf.run(pf.schedules.delete(b("id"))),
                "/api/schedule/run": lambda: pf.call(pf.schedules.run_now, b("id")),
                "/api/schedule/try": lambda: pf.run(pf.schedules.try_once(b("fields") or {}), 60),
                "/api/agent/delete": lambda: delete_agent(pf, b("id")),
                "/api/agent/draft": lambda: pf.run(pf.draft_agent(b("chat")), timeout=120),
                "/api/agent/connect": lambda: connect_agent(b("id"), b("channel")),
                "/api/chat/handoff": lambda: handoff_chat(pf, b("id"), b("channel"), b("fresh")),
                "/api/channel/start": lambda: console.start(b("proc")),
                "/api/channel/stop": lambda: console.stop(b("proc")),
                "/api/channel/restart": lambda: console.restart(b("proc")),
                "/api/channel/disconnect": lambda: console.disconnect(b("path"), b("kind"), b("proc")),
                "/api/global": lambda: save_global(body),
                "/api/dev/try": lambda: pf._dev().try_(str(b("what") or "main")),
                "/api/dev/check": lambda: pf._dev().check(b("prs") or []),
                "/api/perf": lambda: perf.web(b("items"), b("machine")) or {},
                "/api/dev/ship": lambda: pf._dev().ship(b("prs") or [], b("caller")),
                "/api/dev/update": lambda: pf._dev().update(),
                "/api/pick": lambda: {"path": console.pick_folder()},
                "/api/reveal": lambda: console.reveal(b("path")),
                "/api/fs/mkdir": lambda: devices.mkdir(registry.pdir(pf.home), b("path"), b("name")),
                "/api/fs/rename": lambda: devices.rename(registry.pdir(pf.home), b("path"), b("name")),
                "/api/fs/trash": lambda: devices.trash(registry.pdir(pf.home), b("paths") or []),
                "/api/fs/put": lambda: devices.put(registry.pdir(pf.home), b("key"), b("path"), b("name")),
                "/api/fs/scan": lambda: pf.cleaner.start(),
                "/api/fs/clean": lambda: pf.cleaner.clean(b("paths") or []),
                "/api/fs/perm": lambda: devices.open_perm(b("key"), bool(b("reveal"))),
            }
            if u.path in routes:
                return self._api(routes[u.path])
            self._send(404, {"error": "not found"})

        def _upload(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > UPLOAD_LIMIT:
                self.close_connection = True      # 没读的内容不能留在连接里，当成下一个请求
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
            if left > 0:
                os.remove(key)
                raise Problem("文件没传完，再试一次。")
            return {"key": key, "name": name, "size": n}

        def _file_ok(self, chat, path):
            """只给这个对话记录里出现过的文件：Agent 发出来的、用户发过去的。设备管理里的按它的范围（#100）。"""
            if chat == devices.FS_CHAT:
                return devices.allowed_file(path)
            if os.path.isfile(path) and pf.ws_in(chat, path):    # 工作区里的文件（#206）
                return True
            return os.path.isfile(path) and any(
                r.get("type") in ("file", "edit") and path in (r.get("path"), r.get("snap"))   # snap：旧版本的留底，下载用
                or r.get("type") == "user" and any(f.get("path") == path for f in r.get("files") or [])
                or r.get("type") == "chatlog" and any(x.get("image") == path for x in r.get("items") or [])
                for r in pf.channel.logs.read(chat))

        def _fs_ok(self):
            """设备管理里的文件（chat=@fs）能打开主目录里的任何文件：只给这个网页自己（别的网站嵌进来的不给）。"""
            q = parse_qs(urlparse(self.path).query)
            if (q.get("chat") or [""])[0] != devices.FS_CHAT or self.headers.get("X-From-Peer"):
                return True
            return self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")

        def _file(self, chat, path):
            if not self._host_ok() or not self._fs_ok():
                return self._send(403, b"forbidden", "text/plain")
            if not self._file_ok(chat, path):
                return self._send(404, b"not found", "text/plain")
            filesend.serve_local(self, path)

        def _file_part(self, q):
            """别的电脑来取一段（#76）：文件信息 + 这一段。"""
            if not self._guard():
                return
            chat, path = q.get("chat", ""), q.get("path", "")
            if not self._file_ok(chat, path):
                return self._send(404, b"not found", "text/plain")
            try:
                start, n = int(q.get("start") or 0), int(q.get("len") or 0)
            except ValueError:
                return self._send(400, b"bad range", "text/plain")
            self._send(200, filesend.read_part(path, start, n), "application/octet-stream")

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
                while pf.channel.events.alive(q):
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

def check_message(pf, text, files):
    if not (text or "").strip() and not files:
        raise Problem("消息是空的。")
    up = os.path.realpath(os.path.join(registry.pdir(pf.home), "uploads")) + os.sep
    for f in files:
        if not isinstance(f, dict) or not os.path.isfile(f.get("key") or "") or not os.path.realpath(f["key"]).startswith(up):
            raise Problem("文件没传上来，再试一次。")


def diary_of(pf, cid, text, files, acct):
    """/diary 开头的（#363）：记日记，不发给助手。不是的返回 None。"""
    from . import diary as diary_mod
    if diary_mod.parse(text) is None:
        return None
    if acct:
        raise Problem("共享的电脑上不能记日记。")
    if files:
        raise Problem("日记只能记文字。")
    return pf.diary(cid, text)


def slash_skills(q, acct):
    from . import slash
    a = agents.get(q.get("agent")) if q.get("agent") else None
    return slash.listing(os.path.expanduser(q.get("dir") or ""), a, acct or (a or {}).get("account"))


async def usage_report(d):
    from . import usage
    d = os.path.expanduser(d)
    return await usage.report(d if d and os.path.isdir(d) else os.path.expanduser("~"))


def send(pf, cid, text, files, ctx=None, refs=None, acct=None):
    check_message(pf, text, files)
    d = diary_of(pf, cid, text, files, acct)
    if d:
        return d
    if pf.meta(cid).get("mention_of"):       # 在群成员的子对话里开口：另开独立对话（#150），网页跳过去
        cid = pf.call(pf.split_sub, cid)
    pf.bg(pf.send(cid, (text or "").strip(), files, ctx=ctx, refs=refs))
    return {"ok": True, "chat": cid}


def set_members(pf, cid, add, remove):
    from . import groups
    if add and pf.meta(cid).get("mention_of"):
        cid = pf.call(pf.split_sub, cid)
    return {"members": pf.call(groups.set_members, pf, cid, add, remove), "chat": cid}


def answer_secret(pf, sid, password, run, from_peer=False):
    from . import admin
    if run == "dialog":                       # 在另一台电脑上看：在这台 Mac 上弹系统授权窗口
        password = admin.DIALOG
    elif run and from_peer:
        raise Problem("开机密码只能在这台电脑自己的网页里输。")
    elif run and not password:
        raise Problem("先输入开机密码。")
    if not pf.channel.answer_secret(sid or "", password if run else None):
        raise Problem("这张卡片已经处理过或作废了。")
    return {}


def list_dirs(path, top=None):
    """网页里选另一台电脑上的文件夹：列出子文件夹。top：共享电脑上的访客只能在它的专属文件夹里选（#154）。"""
    home = top or os.path.expanduser("~")
    d = os.path.realpath(os.path.expanduser(path or "~"))
    if top and not share.inside(d, top):
        d = top
    if not os.path.isdir(d):
        raise Problem("找不到这个文件夹。")
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith(".") and os.path.isdir(os.path.join(d, n)))
    except PermissionError:
        raise Problem("没有权限打开这个文件夹。")
    return {"path": d, "parent": os.path.dirname(d) if d != "/" and d != top else None, "home": home, "dirs": names[:500]}


def _agent(aid):
    a = agents.get(aid or "")
    if not a:
        raise Problem("找不到这个 Agent。")
    return a


def skill_write(pf, fn, *args):
    """改了平台 Skill：叫别的电脑马上来同步，网页刷新。"""
    r = fn(*args)
    skills_mod.changed()
    return r if isinstance(r, dict) else {"result": r}


def install_skill(pf, body):
    if body.get("url"):
        names = skills_mod.install_github(body["url"])
    elif body.get("key"):
        names = skills_mod.install_upload(body["key"], registry.pdir(pf.home), body.get("name") or "")
    elif body.get("local"):
        names = skills_mod.import_local(body["local"], pf.peers.me.get("name") or "", pf.peers.me.get("id") or "")
    else:
        raise Problem("选一个来源。")
    return {"names": names}


def leave_hub(pf):
    pf.peers.leave()
    from . import runtime
    runtime.schedule_restart(pf.home, delay=2)
    return {}


def mention(pf, cid, text, to, files, acct=None):
    check_message(pf, text, files)
    d = diary_of(pf, cid, text, files, acct)
    if d:
        return d
    if pf.meta(cid).get("mention_of"):
        cid = pf.call(pf.split_sub, cid)
    return dict(pf.run(pf.mentions.send(cid, (text or "").strip(), to, files)), chat=cid)


def _busy_of(a):
    """这个 Agent 在各处（网页、飞书、微信）有没有在干活，在干什么。"""
    from . import web as console
    rows = []
    for kind, proc, path in agents.linked_paths(a["id"]):
        rows += [c for c in console._all_chats(proc) if c.get("busy")]
    return rows


def title_text(text):
    """用户的话当标题：网页的引用（> 开头的行）拼在最前面，有正文用正文，只有引用就用引用的字。"""
    body = re.sub(r"^(?:>[^\n]*(?:\n|$))*", "", text or "").strip()
    return " ".join((body or re.sub(r"^>\s?", "", text or "", flags=re.M)).split())


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
        view["chats"] = _agent_chats(pf, aid) + pf.external_chats(aid, terminal=True)
        view["chats"].sort(key=lambda r: r.get("last_active") or "", reverse=True)
    return view


def _agent_chats(pf, aid):
    """Agent 页左边列表里它的网页对话（#359）：它参与的群聊列群聊本身（和会话页同一个对话），不列它在里面的子对话；
    只是成员的标 member。侧边对话里被 @ 的那份不列；别的电脑上的群聊（本机没有外层）照旧列子对话。"""
    chats, rows, seen = pf.chats(), [], set()
    for cid, c in chats.items():
        if c.get("agent") == aid and c.get("mention_of") and c["mention_of"] in chats:
            cid = c["mention_of"]
            if not board_mod.top_level(chats[cid], cid):
                continue
        elif c.get("agent") != aid and not (aid in (c.get("members") or []) and board_mod.top_level(c, cid)):
            continue
        if cid in seen:
            continue
        seen.add(cid)
        v = pf.chat_view(cid)
        if v.get("agent") != aid:
            v["member"] = True
        rows.append(v)
    return rows


def agent_recent(pf, aid, acct=None, n=5):
    """头像卡片（#298、#307）：这个 Agent 最近的任务。它自己的对话、它在的群聊、在别的对话里被 @ 时做的那份，
    再加飞书、微信里的；不含归档。{rows, total}，被 @ 的那份带 of（在哪个对话里）。"""
    if not agents.get(aid):
        raise Problem("找不到这个 Agent。")
    ok, chats, picks = share.chat_filter(pf, acct), pf.chats(), []
    for cid, m in chats.items():
        if m.get("archived") or not ok(cid, m.get("agent")):
            continue
        if m.get("agent") == aid and m.get("mention_of"):
            picks.append((m.get("last_active") or "", cid, m["mention_of"]))
        elif board_mod.top_level(m, cid) and (m.get("agent") == aid or aid in (m.get("members") or [])):
            picks.append((m.get("last_active") or "", cid, None))
    ext = [r for r in pf.external_chats(aid) if ok(r["id"], aid) and not r.get("archived")]
    picks += [(r.get("last_active") or "", r["id"], r) for r in ext]
    picks.sort(key=lambda t: t[0], reverse=True)
    rows = []
    for _, cid, x in picks[:n]:
        if isinstance(x, dict):
            rows.append(x)
            continue
        r = pf.chat_view(cid)
        if x:
            r["of"] = {"id": x, "title": (chats.get(x) or {}).get("title") or "对话"}
        rows.append(r)
    return {"rows": rows, "total": len(picks)}


def state(pf, acct=None):
    """acct：共享电脑上访客的账号，只给它自己的；主人（None）看不到访客的（#154）。"""
    from . import models
    from . import web as console
    g = registry.global_config()
    from . import upgrade
    chats, more = pf.chat_head(acct)
    return share.filter_state(pf, {
        "ccim": dict(upgrade.status(), **({"dev": pf.dev.badge()} if pf.dev else {})), "agents": [agent_view(pf, aid) for aid in agents.all_agents()],
        "chats": chats, "chat_more": more,
        "projects": pf.projects.summary(), "schedules": pf.schedules.summary(),
        "global": g, "defaults": {"claude": _defaults("claude", os.path.expanduser("~")),
                                  "codex": _defaults("codex", os.path.expanduser("~"))},
        "codex": console._codex(), "home": os.path.expanduser("~"), "perms": devices.perms(),
        "caps": ["side_at"],                  # 别的电脑看这台支持什么（side_at：侧边对话里被 @ 时只读，#290）
        "models": {"claude": models.claude(), "codex": models.codex()}}, acct)


def save_agent(pf, aid, fields, acct=None):
    if not acct:                              # 账号只在访客新建时由 share.check 定
        fields.pop("account", None)
    src = None if aid else fields.pop("from_chat", None)
    fields.pop("from_chat", None)
    if src:                                   # 临时对话保存为 Agent（#318）：底座跟对话一致，否则会话接不上
        base, path, chosen = pf.call(pf.adoptable, src)
        fields["base"] = base
        if not fields.get("dir") and not chosen:
            fields["dir"] = path              # 没选过目录：它的临时目录就是 Agent 的工作目录，做出来的文件都在
            os.makedirs(path, exist_ok=True)
    if fields.get("base") == "codex":
        from . import web as console
        if not console._codex():
            raise Problem("这台电脑上还没装 Codex。装好 ChatGPT 桌面版后再选。")
    if fields.get("dir"):
        d = os.path.realpath(os.path.expanduser(fields["dir"].strip()))
        if not os.path.isdir(d):
            raise Problem("找不到这个工作目录。")
        if d == "/":
            raise Problem("工作目录不能是整个硬盘，选一个具体的文件夹。")
        if d == os.path.realpath(os.path.expanduser("~")) and not fields.get("confirmed"):
            return {"confirm": "home"}        # 主目录：网页上说明影响，用户确认后再带 confirmed 保存（#50）
    apply = fields.pop("apply", None)
    if aid:
        before = agents.get(aid) or {}
        new = agents.clean(fields)
        keys = [] if new.get("base", before.get("base")) != before.get("base") else \
            [k for k in ("model", "effort") if k in new and new[k] != before.get(k)]
        old_dir = agents.cwd(before)
        moved = "dir" in new and (new["dir"] or None) != (before.get("dir") or None)
        dir_ok = fields.pop("dir_ok", None)
        n = len(pf.call(pf.agent_chats, aid)) if moved and not dir_ok else 0
        if n:                                 # 换目录：告诉用户聊过的对话还在原来的目录（#387）
            return {"confirm": "dir", "count": n, "dir": old_dir}
        n = len(pf.call(pf.agent_chats, aid, keys, True)) if keys and apply not in ("live", "new") else 0
        if n:
            return {"confirm": "apply", "count": n}   # 网页上选用不用到已开的对话（#387）
        if moved:
            pf.call(pf.keep_dir, aid, old_dir)
        if keys:
            agents.mark_apply(aid, "new" if apply == "new" else "live")   # 先写，飞书、微信看到新设置时就知道怎么办
        a = agents.update(aid, **fields)
        if keys:
            pf.call(pf.apply_agent, aid, keys, before, apply != "new")
        if bool(a.get("chrome")) != bool(before.get("chrome")) or (a.get("role") or "") != (before.get("role") or ""):
            pf.call(pf.relaunch_agent, aid)
    else:
        a = agents.create(**fields)
        if src:
            pf.run(pf.adopt_chat(src, a["id"]))
    return {"id": a["id"], "chat": src}


def delete_agent(pf, aid):
    from . import web as console
    a = agents.get(aid)
    if not a:
        return {}
    if any(c.get("agent") == aid and c.get("busy") for c in pf.chats().values()):
        raise Problem("它还在干活，先停下再删。")
    for kind, proc, path in agents.linked_paths(aid):
        console.disconnect(path, kind, proc)
    for pid in list(pf.projects.all()):       # 从它在的项目里移出
        pf.call(pf.projects.member, pid, None, aid)
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


def handoff_chat(pf, cid, kind, fresh=False):
    """网页对话转到这个 Agent 的微信 / 飞书私聊接着聊（#212）。渠道没在跑就先启动。"""
    from . import daemon, handoff
    from . import web as console
    prep = pf.call(pf.handoff_prep, cid, kind)
    if prep.get("need_connect"):
        return prep
    proc, label = prep["proc"], "微信" if kind == "weixin" else "飞书"
    started = not daemon.running(proc)
    if started:
        console.start(proc)
    elif kind == "weixin" and "weixin-handoff" not in (registry.read_state(proc).get("caps") or []):
        raise Problem("微信那边要先重启一下：在 Agent 设置里点微信的「重启」，再转。")
    res = handoff.request(proc, dict(prep["req"], fresh=bool(fresh)), timeout=90 if started else 30)
    if not res.get("ok"):
        raise Problem(f"没转过去：{res.get('error') or '没有响应'}")
    where = "微信" if kind == "weixin" else f"飞书「{(registry.get(proc) or {}).get('bot_name') or '机器人'}」"
    pf.call(pf.handoff_note, cid, label)
    return {"ok": True, "where": where, "label": label}


def start_upgrade(force):
    """网页上点了升级（#37）：有对话在干活先问一句。"""
    from . import upgrade
    if not force:
        b = upgrade.busy()
        if b:
            return {"busy": b}
    try:
        return {"job": upgrade.start()}
    except ValueError as e:
        raise Problem(str(e))


def kb_write(pf, fn, *args):
    try:
        r = fn(*args)
    except ValueError as e:
        raise Problem(str(e))
    pf.kb_changed()
    return r


def archive_days():
    """已完成的对话多少天没有新消息就清理；0 = 不清理。"""
    v = registry.global_config().get("archive_days") or "30"
    return 0 if v == "never" else int(v) if str(v).isdigit() else 30


def delete_at(m, days):
    """已完成的对话什么时候清理（时间戳）：标完成时、最后一条消息，取晚的那个，再过 days 天。没标完成或不清理返回 None。"""
    if not days or not m.get("archived"):
        return None
    last = 0
    if m.get("last_active"):
        try:
            last = time.mktime(time.strptime(m["last_active"][:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    return max(m.get("archived_at") or time.time(), last) + days * 86400


def set_workspace(on):
    """工作区（#245）：在设置里打开后才能用，每台电脑各存一份。"""
    registry.set_global(workspace="on" if on else None)
    return {"workspace": on}


def set_diary_cards(on):
    """聊天里显示不显示日记卡片（#363），默认显示；只影响网页上的显示，日记照记。"""
    registry.set_global(diary_cards=None if on else "off")
    return {"diary_cards": on}


DIARY_STYLES = ("note", "paper")   # 不在里面的都按默认的卡片（#378）


def set_diary_style(v):
    """聊天里日记卡片的样式（#378）：卡片（默认）、便签、横线纸。"""
    v = v if v in DIARY_STYLES else None
    registry.set_global(diary_style=v)
    return {"diary_style": v}


POCKET = {"on": True, "base": "claude", "model": None, "effort": None}


def pocket_settings():
    """右下角助理的设置（#294）：开不开、新开对话时默认的底座、模型、思考深度（空 = 跟随全局默认）。"""
    g = registry.global_config().get("pocket") or {}
    return {**POCKET, **{k: g[k] for k in POCKET if k in g}}


def set_pocket(body):
    s = pocket_settings()
    if "on" in body:
        s["on"] = bool(body["on"])
    if "base" in body:
        if body["base"] not in ("claude", "codex"):
            raise Problem("没有这个底座。")
        if body["base"] != s["base"]:
            s.update(model=None, effort=None)  # 两个底座的模型名和档位不一样
        s["base"] = body["base"]
    for k in ("model", "effort"):
        if k in body:
            s[k] = str(body[k] or "").strip()[:60] or None
    registry.set_global(pocket=s)
    return {"pocket": s}


def set_archive_days(v):
    if v not in ("7", "30", "90", "never"):
        raise Problem("没有这个选项。")
    registry.set_global(archive_days=v)
    return {"archive_days": v}


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
