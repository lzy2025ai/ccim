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
from urllib.parse import parse_qs, urlparse

from . import agents, registry
from . import devices, docs, filesend, share
from . import kb as kb_mod
from . import skills as skills_mod
from . import board as board_mod
from .schedules import is_quiet as schedules_quiet, listed
from .channels.base import CardAction, Channel, Incoming, inbox_path

log = logging.getLogger("ccim.platform")

PORT = 8977
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
MARKS = ("title", "pin", "star", "archived", "archived_at", "icon", "color", "hidden")          # 对话列表里用户加的标记（star：星标，#194）
ICONS = ["star", "flag", "bolt", "bug", "heart", "bookmark"]
COLORS = ["red", "orange", "yellow", "green", "blue", "purple"]
# 页面专用的助手（#116）
GUEST_HELPER = ("\n- 这个用户用的是别人共享给他的电脑：你装、改、删的是他自己的那一份 Skill，只有他自己的 Agent 能用，不会同步到别的电脑。"
                "skill_list 里标了 shared 的是电脑主人的，只能看、能用，不能改也不能删。只能从 GitHub 地址安装。")
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
# 旁问（#196）：主对话干活时，在侧栏里另开一个对话问问题，不打断它
SIDE_ROLE = ("\n\n## 旁问\n"
             "你在「旁问」侧栏里：用户的主对话可能正在干活，用户不想打断它，来这里问问题。你是从主对话分出来的，知道之前聊过的内容；"
             "用户消息前面括号里是平台自动附上的（不是用户贴的）：主对话在那之后的新进展、正在进行的那一轮做到哪了。\n"
             "- 你只能看、不能动：可以读文件、搜索、跑只看不改的命令（git status、git log、查日志等）；"
             "不删改文件、不装东西、不提交推送、不启动或停止进程，用户明确要求也不做，现在不做、以后也不做。\n"
             "- 用户要你动手时，说明这里只能问问题，请他回主对话说，可以把要对主对话说的话写好给他。\n"
             "- 回答简短。")
SIDE_CTX = 6000                                                    # 每次带上的主对话新进展，最多这么多字
MERGED = ("card", "reply", "run", "mention", "secret")                   # 记录里会被更新的：同一个 id 后来的覆盖前面的


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
                    if r.get("type") in MERGED and r.get("id") in pos:
                        old = out[pos[r["id"]]]
                        # 卡片每次都是整张：处理过的卡片没有按钮，合并的话旧按钮会留下，看着还在等批准（#48）
                        out[pos[r["id"]]] = {**(r if r["type"] == "card" else {**old, **r}), "at": old["at"]}
                        continue
                    if r.get("type") in MERGED:
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

    def __init__(self, project_path, entry, secret, handlers):
        super().__init__(project_path, entry, secret, handlers)
        self.events = Events()
        self.logs = Logs(os.path.join(registry.pdir(project_path), "web"))
        self.progress_of = {}                 # 对话 → 正在干活的进度
        self.done_steps = {}                  # 对话 → 刚做完那一轮的步骤，接下来的回答带上它
        self.card_chat = {}                   # 卡片 id → 对话
        self.secrets = {}                     # 等用户输密码的卡片 id → (对话, future)
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

    async def progress(self, chat_id, snap):
        if snap["state"] == "working":
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
        else:
            rec = {"type": "note", "text": markdown or ""}
        self._emit(chat_id, self.logs.append(chat_id, rec))
        if rec["type"] == "answer" and getattr(self, "on_answer", None):
            self.on_answer(chat_id)

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
        rec = {"type": "file", "path": os.path.realpath(path), "name": os.path.basename(path), "size": os.path.getsize(path)}
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
        self.background = set()
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
        return {"attention": board_mod.attention(m, summ), "unread": board_mod.unread(m, summ),
                "ask": board_mod.ask_preview(summ.get("last_user"))}

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

    def relaunch_agent(self, aid):
        """Agent 改了要换启动参数的设置（chrome）：它已经开着的对话下一条消息时按原会话续接，带上新参数。"""
        for key, b in self.bridges.items():
            if key == aid or key.startswith(aid + "@"):
                for c in b.chats.values():
                    c.reconnect = True

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

    # ----- 归档的对话到期删除（#26） -----

    async def _cleanup_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self.cleanup_archived()
            except Exception:
                log.exception("清理归档的对话出错")
            await asyncio.sleep(3600)

    async def cleanup_archived(self):
        days = archive_days()
        if not days:
            return
        now = time.time()
        chats = self.chats()                  # 以前归档的没记归档时间：从现在算起
        if any(m.get("archived") and not m.get("archived_at") for m in chats.values()):
            for m in chats.values():
                if m.get("archived") and not m.get("archived_at"):
                    m["archived_at"] = now
            self.store.write(chats)
        for cid, m in list(self.chats().items()):
            if m.get("archived") and not m.get("busy") and (delete_at(m, days) or now + 1) <= now:
                log.info("归档的对话到期，删除：%s", m.get("title") or cid)
                await self.delete_chat(cid)
        marks = registry.read_state(self.home).get("marks") or {}
        if any(mk.get("archived") and not mk.get("archived_at") for mk in marks.values()):
            for mk in marks.values():
                if mk.get("archived") and not mk.get("archived_at"):
                    mk["archived_at"] = now
            registry.write_state(self.home, marks=marks)
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
            if m.get("side_of"):              # 旁问（#196）：只读，单独一个 Bridge
                key += "|side"
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
            if r.get("type") in ("user", "answer", "file", "note", "mention"):
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

    async def send(self, cid, text, files=(), ctx=None, refs=None):
        b = self._bridge(cid)
        m = self.meta(cid)
        refs = self._refs(cid, refs)
        if m.get("fork_from") and cid not in b.chats:
            b.chat(cid, "p2p", None if m.get("fork_at") else m["fork_from"])
        mid = "u-" + uuid.uuid4().hex[:10]
        shown = [{"name": f["name"], "size": f.get("size"), "path": os.path.realpath(f["key"])} for f in files]
        self.channel._emit(cid, self.channel.logs.append(cid, {"type": "user", "id": mid, "text": text, "files": shown,
                                                               **({"refs": refs} if refs else {})}))
        if text and not m.get("title"):       # 用用户的原话当标题：交给助手的会拼上引用说明等前缀
            chats = self.chats()
            if cid in chats and not chats[cid].get("title"):
                chats[cid]["title"] = " ".join(text.split())[:30]
                self.store.write(chats)
        from .bridge import QUEUE
        head = re.match(r"(?:>[^\n]*\n)*\s*", text or "").end()   # 网页的引用拼在最前面，跳过它再认（#117）
        q = QUEUE.match((text or "")[head:])   # 前面还要拼别的内容，所以 /queue 在这里认出来
        if q:
            text = text[:head] + text[head + q.end():]
        if not m.get("mention_of"):           # 刚才 @ 的 Agent 的回答，原来的助手还没看过：带上；群聊说明改过也带上
            from . import groups
            text = groups.note_prefix(self, cid, groups.MAIN) + self.mentions.unseen(cid) + self._doc_edits(cid) + text if text else text
        if m.get("side_of") and text:          # 旁问：带上主对话的新进展
            text = self._side_ctx(cid, m) + text
        if refs and text:                     # 用户指了以前的对话：告诉它在哪读，不用自己搜
            from .search import short
            text = "（用户指了你以前的这些对话，和这次说的有关，用 open_chat 读，不用再搜：" + \
                "；".join(f"「{r['title']}」id={short(r['id'])}" for r in refs) + "）\n\n" + text
        if ctx and m.get("helper") and text:      # 助手要知道用户在页面上正看着什么（不记进对话记录）
            text = f"（{str(ctx)[:300]}）\n{text}"
        inc = Incoming(chat_id=cid, chat_type="p2p", sender_id=OWNER, message_id=mid, text=text,
                       files=[("file", f["key"], f["name"]) for f in files], kind="text" if text else "file", queue=bool(q))
        await b.on_message(inc)

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

    async def side_chat(self, cid, fresh=False):
        """旁问（#196）：这个对话的旁问对话，一个对话一个，不进对话列表；没有或 fresh 就从主对话最后一条回答处分一个新的。"""
        m = self.meta(cid)
        if m.get("side_of") or m.get("helper") or m.get("mention_of") or cid.startswith("x:"):
            raise Problem("这个对话不能旁问。")
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
        new = self.new_chat(agent=m.get("agent"), temp=dict(t) if t else None, title="旁问",
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
        """旁问每次发消息时带上的：主对话在上次之后的新内容，和正在进行的那一轮做到哪了。"""
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
        p = self.channel.progress_of.get(of)
        if p:
            steps = p.get("steps") or []
            now = "正在进行的这一轮：" + ("已经做了 " + "；".join(steps[-15:]) if steps else "刚开始")
            if (p.get("text") or "").strip():
                now += "\n正在写的回答：" + p["text"].strip()[-1500:]
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
        for c in [c for c, x in chats.items() if x.get("side_of") == cid]:   # 它的旁问一起删
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
        model = (c or {}).get("model") or (a or t).get("model") or g.get(px + "model")
        effort = (c or {}).get("effort") or (a or t).get("effort") or g.get(px + "effort")
        dm, de = _defaults(base, agents.cwd(a) if a else (t.get("dir") or os.path.expanduser("~")))
        return {"base": base, "model": model or dm, "effort": effort or de,
                "model_custom": bool((c or {}).get("model")), "effort_custom": bool((c or {}).get("effort"))}

    def _fork_title(self, cid, m):
        """分叉出来、还没开口的对话：用分叉那条回答之前的问题当标题；开口后第一句话就是标题。"""
        if not m.get("parent"):
            return None
        q = next((r.get("text") for r in reversed(self.channel.logs.read(cid)) if r.get("type") == "user" and r.get("text")), "")
        q = " ".join(q.split())
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
                "listed": listed(cid, m),
                "origin_source": m.get("source"),
                "parent": m.get("parent") or ("x:" + m["origin"] if m.get("origin") and not self._linked(m) else None),
                **{k: m.get(k) for k in MARKS if k != "title"}, "delete_at": delete_at(m, archive_days()), "kbs": m.get("kbs") or [],
                **self._board_marks(cid, m), "issue": self._issue(cid, m),
                "brief": m.get("brief"), "doing": ((self.channel.progress_of.get(cid) or {}).get("steps") or [None])[-1],
                "stall": self._stall(cid),
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
        v["progress"] = self.channel.progress_of.get(cid)
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

    def chat_list(self):
        rows = [self.chat_view(cid) for cid, c in self.chats().items() if board_mod.top_level(c, cid)] + \
            self.external_chats()
        rows.sort(key=lambda r: r.get("last_active") or "", reverse=True)
        return rows

    # ----- 列表里的标记：重命名、置顶、星标、归档、图标、颜色、从列表里去掉 -----

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
        if changes.get("pin") is True:
            changes["pin"] = -time.time()         # 新置顶的排在最上面
        if "archived" in changes:
            changes["archived_at"] = time.time() if changes["archived"] else None
        if cid.startswith("x:"):
            marks = registry.read_state(self.home).get("marks") or {}
            m = {**marks.get(cid, {}), **changes}
            marks[cid] = {k: v for k, v in m.items() if v not in (None, False, "")}
            registry.write_state(self.home, marks=marks)
            return {}
        if "hidden" in changes:
            raise Problem("网页里的对话直接删除。")
        chats = self.chats()
        if cid not in chats:
            raise Problem("找不到这个对话。")
        for k, v in changes.items():
            if v in (None, False, ""):
                chats[cid].pop(k, None)
            else:
                chats[cid][k] = v
        self.store.write(chats)
        return self.chat_view(cid)

    def set_chapter(self, cid, key, name):
        """章节（#25）：key 是消息的「类型:时间」，name 为空就取消。"""
        if not isinstance(key, str) or not re.fullmatch(r"(user|answer):[\d.]+", key):
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

    def doc_save(self, cid, path, text, mtime):
        self.doc_versions(cid, path)
        if not isinstance(text, str):
            raise Problem("内容不对。")
        try:
            docs.save(path, text, mtime)
        except docs.Problem as e:
            raise Problem(str(e))
        if cid == devices.FS_CHAT:
            return self.doc_view(cid, path)
        rec = {"type": "edit", "path": path, "name": os.path.basename(path)}
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
        raise Problem("找不到这个对话。")

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

    def fork_chat(self, cid, index, src="log"):
        """从这个对话的第 index 条回答分出一个新对话：之前的内容带过去，之后两边各聊各的。
        src=earlier：从接着聊之前（终端、桌面端里）的回答分；终端、桌面端的对话（x:）直接按记录里的编号分。"""
        if cid.startswith("x:") or src == "earlier":
            return self._fork_history(cid, index)
        m = self.meta(cid)
        if m.get("busy"):
            raise Problem("等它这一轮做完再分叉。")
        rows = self.channel.logs.read(cid)
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
            if r.get("type") in ("user", "answer", "file", "note", "mention"):
                self.channel.logs.append(new, r)      # 带着 fork：在新对话里也能从之前的回答再分
        return {"id": new}

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

    async def rewind_preview(self, cid, index):
        """退回第 index 条时会恢复哪些文件（相对工作目录）。"""
        m = self.meta(cid)
        rows = self.channel.logs.read(cid)
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

    async def rewind_chat(self, cid, index, files):
        m = self.meta(cid)
        if m.get("busy"):
            raise Problem("它还在回答，先停下再退回。")
        rows = self.channel.logs.read(cid)
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

    async def peer_task(self, cid, aid, prompt, title, project, kind=None, of=None, shown=None, depth=0):
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
                          **({"mention_of": of} if kind == "mention" else {"project": project})}
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
        if r is not None:
            self.peer_results.pop(token, None)
        return r or {"done": False}

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
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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

        def do_GET(self):
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
                "/api/state": lambda: state(pf, acct) if q.get("local") else pf.peers.merged_state(state(pf)),
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
                "/api/board": lambda: pf.call(board_mod.stats, pf, lambda cid: share.owner_map(pf).get(cid) == acct),
                "/api/dev": lambda: pf._dev().overview(fresh=bool(q.get("fresh"))),
                "/api/chat": lambda: pf.chat_detail(q.get("id", "")),
                "/api/chat/sig": lambda: pf.chat_sig(q.get("id", "")),
                "/api/doc": lambda: pf.doc_view(q.get("chat", ""), q.get("path", ""), q.get("v")),
                "/api/agent": lambda: share.filter_agent_chats(pf, agent_view(pf, q.get("id", ""), detail=True), acct),
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
            }
            if u.path in routes:
                return self._api(routes[u.path])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if self._remote():
                return
            if self.headers.get("X-From-Peer") and u.path in ("/api/pick", "/api/reveal"):
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
            acct, denied = self._guest("POST", u.path, {}, body)
            if denied:
                return
            b = body.get
            routes = {
                "/api/chat/new": lambda: {"id": pf.call(pf.new_chat, agent=b("agent"), temp=b("temp"), account=acct)},
                "/api/chat/send": lambda: send(pf, b("id"), b("text") or "", b("files") or [], b("ctx"), b("refs")),
                "/api/helper": lambda: {"id": pf.run(pf.helper_chat(b("kind"), bool(b("fresh")), acct))},
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
                "/api/skills/update": lambda: skill_write(pf, skills_mod.update, b("name")),
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
                "/api/doc/save": lambda: pf.call(pf.doc_save, b("chat"), b("path"), b("text"), b("mtime")),
                "/api/chat/chapter": lambda: pf.call(pf.set_chapter, b("id"), b("key"), b("name")),
                "/api/chat/fork": lambda: pf.call(pf.fork_chat, b("id"), b("index"), b("src") or "log"),
                "/api/chat/rewind_preview": lambda: pf.run(pf.rewind_preview(b("id"), b("index")), timeout=90),
                "/api/chat/rewind": lambda: pf.run(pf.rewind_chat(b("id"), b("index"), bool(b("files"))), timeout=120),
                "/api/peer/task": lambda: pf.run(pf.peer_task(b("chat"), b("agent"), b("prompt") or "", b("title") or "项目",
                                                              b("project") or "", b("kind"), b("of"), b("shown"), b("depth"))),
                "/api/chat/members": lambda: set_members(pf, b("id"), b("add") or [], b("remove") or []),
                "/api/chat/note": lambda: pf.call(groups_mod.set_note, pf, b("id"), b("text") or ""),
                "/api/peer/result": lambda: pf.call(pf.peer_result, b("token") or ""),
                "/api/agent/pin": lambda: (agents.update(b("id"), pinned=bool(b("on"))), {})[1],
                "/api/chat/mention": lambda: mention(pf, b("id"), b("text") or "", b("to") or [], b("files") or []),
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
                "/api/agent/connect": lambda: connect_agent(b("id"), b("channel")),
                "/api/channel/start": lambda: console.start(b("proc")),
                "/api/channel/stop": lambda: console.stop(b("proc")),
                "/api/channel/restart": lambda: console.restart(b("proc")),
                "/api/channel/disconnect": lambda: console.disconnect(b("path"), b("kind"), b("proc")),
                "/api/global": lambda: save_global(body),
                "/api/dev/try": lambda: pf._dev().try_(str(b("what") or "main")),
                "/api/dev/check": lambda: pf._dev().check(b("prs") or []),
                "/api/dev/ship": lambda: pf._dev().ship(b("prs") or []),
                "/api/dev/update": lambda: pf._dev().update(),
                "/api/pick": lambda: {"path": console.pick_folder()},
                "/api/reveal": lambda: console.reveal(b("path")),
                "/api/fs/mkdir": lambda: devices.mkdir(registry.pdir(pf.home), b("path"), b("name")),
                "/api/fs/rename": lambda: devices.rename(registry.pdir(pf.home), b("path"), b("name")),
                "/api/fs/trash": lambda: devices.trash(registry.pdir(pf.home), b("paths") or []),
                "/api/fs/put": lambda: devices.put(registry.pdir(pf.home), b("key"), b("path"), b("name")),
                "/api/fs/scan": lambda: pf.cleaner.start(),
                "/api/fs/clean": lambda: pf.cleaner.clean(b("paths") or []),
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
            return os.path.isfile(path) and any(
                r.get("type") in ("file", "edit") and path in (r.get("path"), r.get("snap"))   # snap：旧版本的留底，下载用
                or r.get("type") == "user" and any(f.get("path") == path for f in r.get("files") or [])
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


def send(pf, cid, text, files, ctx=None, refs=None):
    check_message(pf, text, files)
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
        names = skills_mod.import_local(body["local"], pf.peers.me.get("name") or "")
    else:
        raise Problem("选一个来源。")
    return {"names": names}


def leave_hub(pf):
    pf.peers.leave()
    from . import runtime
    runtime.schedule_restart(pf.home, delay=2)
    return {}


def mention(pf, cid, text, to, files):
    check_message(pf, text, files)
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


def state(pf, acct=None):
    """acct：共享电脑上访客的账号，只给它自己的；主人（None）看不到访客的（#154）。"""
    from . import models
    from . import web as console
    g = registry.global_config()
    from . import upgrade
    return share.filter_state(pf, {
        "ccim": dict(upgrade.status(), **({"dev": pf.dev.badge()} if pf.dev else {})), "agents": [agent_view(pf, aid) for aid in agents.all_agents()], "chats": pf.chat_list(),
        "projects": pf.projects.summary(), "schedules": pf.schedules.summary(),
        "global": g, "defaults": {"claude": _defaults("claude", os.path.expanduser("~")),
                                  "codex": _defaults("codex", os.path.expanduser("~"))},
        "codex": console._codex(), "home": os.path.expanduser("~"),
        "models": {"claude": models.claude(), "codex": models.codex()}}, acct)


def save_agent(pf, aid, fields, acct=None):
    if not acct:                              # 账号只在访客新建时由 share.check 定
        fields.pop("account", None)
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
    if aid:
        before = (agents.get(aid) or {}).get("chrome")
        a = agents.update(aid, **fields)
        if bool(a.get("chrome")) != bool(before):
            pf.call(pf.relaunch_agent, aid)
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
    """归档的对话多少天没有新消息就删除；0 = 不删除。"""
    v = registry.global_config().get("archive_days") or "30"
    return 0 if v == "never" else int(v) if str(v).isdigit() else 30


def delete_at(m, days):
    """归档的对话什么时候删除（时间戳）：归档时、最后一条消息，取晚的那个，再过 days 天。没归档或不删除返回 None。"""
    if not days or not m.get("archived"):
        return None
    last = 0
    if m.get("last_active"):
        try:
            last = time.mktime(time.strptime(m["last_active"][:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    return max(m.get("archived_at") or time.time(), last) + days * 86400


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
