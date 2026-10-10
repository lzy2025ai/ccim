"""微信号：一个微信号只能有一个 ClawBot，所以微信按「一个号管多个项目」运行。

登记表里每个微信号一条，键是 ~/.ccim/weixin/<名字>（kind=weixin），记着登录信息和它管的项目（projects）；
daemon、安全重启、ccim start / stop / logs 都把这个目录当成「项目路径」用，不用另写一套。
每个号一个 ccim 进程，进程里每个项目一个 Bridge，消息交给当前项目，在微信里用 /项目 切换；
切走的项目照常在后台干活，回答前面加「【项目名】」。
各项目在微信里的会话记录存在这个号的 state.json 的 chats[项目路径]：飞书进程也在写项目自己的 state.json，
两个进程写同一个文件会互相覆盖。当前项目记在这个号的 state.json 的 current。
"""
import logging, os, re, shutil, time

from . import daemon, registry

log = logging.getLogger("ccim.weixin")

SETTINGS = ("model", "effort", "agent")
SWITCH = ("/项目", "/project")
PICK_WINDOW = 300          # 发出项目列表后，这么多秒内直接回序号（或项目名）就是切换


def hub_path(name):
    return os.path.join(registry.HOME, "weixin", name)


def hubs():
    return [(p, e) for p, e in registry.all_projects().items() if e.get("kind") == "weixin"]


def hubs_of(path):
    path = os.path.realpath(path)
    return [(p, e) for p, e in hubs() if path in (e.get("projects") or {})]


def find(name=None):
    """按名字找微信号，返回 (路径, 条目)；不写名字时只有一个号就是它。没有返回 (None, None)。"""
    hs = hubs()
    if name:
        return next(((p, e) for p, e in hs if e.get("bot_name") == name), (None, None))
    if len(hs) > 1:
        raise SystemExit(f"你登录了好几个微信号：{'、'.join(e.get('bot_name') for _, e in hs)}。写上名字指定是哪个。")
    return hs[0] if hs else (None, None)


def _new_name():
    names = {e.get("bot_name") for _, e in hubs()}
    return next(n for n in ["微信"] + [f"微信{i}" for i in range(2, 100)] if n not in names)


def login(name=None, show=None):
    """扫码登录一个微信号。扫的是已经登录过的微信（同一个人）就更新它的登录，项目都还在。返回 (路径, 条目)。"""
    from .channels.weixin import scan_login, show_in_terminal
    if name and not re.fullmatch(r"[^/\\\s]{1,20}", name):
        raise SystemExit("名字不能带空格和斜杠，20 个字以内。")
    got = scan_login(show=show or show_in_terminal)
    if not got:
        raise SystemExit("没有登录成功。")
    old = next(((p, e) for p, e in hubs() if e.get("owner_open_id") == got["user_id"]), None)
    if old:
        path, e = old
        if e.get("app_id") != got["account_id"]:
            registry.del_secret(e["app_id"])
        registry.set_secret(got["account_id"], got["token"])
        e = registry.update(path, app_id=got["account_id"], base_url=got["base_url"], paired_at=registry.now())
        print(f"\n这个微信之前登录过（「{e['bot_name']}」），已更新登录，原来的项目都还在。")
        return path, e
    name = name or _new_name()
    if find(name)[0]:
        raise SystemExit(f"已经有一个叫「{name}」的微信号了，换个名字。")
    path = hub_path(name)
    taken = registry.all_projects()
    for i in range(2, 100):                       # 目录名不跟着改名，改过名的号可能还占着这个目录
        if path not in taken:
            break
        path = hub_path(f"{name}-{i}")
    os.makedirs(path, exist_ok=True)
    registry.set_secret(got["account_id"], got["token"])
    e = {"channel": "weixin", "kind": "weixin", "bot_name": name, "app_id": got["account_id"], "base_url": got["base_url"],
         "owner_open_id": got["user_id"], "projects": {}, "paired_at": registry.now()}
    registry.put(path, e)
    print(f"\n已登录微信「{name}」。")
    return path, registry.get(path)


def rename(old, new):
    """只改名字；目录不动（daemon、开机自启、状态文件都按目录登记）。"""
    hub, e = find(old)
    if not hub:
        raise SystemExit(f"没有叫「{old}」的微信号。")
    if not re.fullmatch(r"[^/\\\s]{1,20}", new):
        raise SystemExit("名字不能带空格和斜杠，20 个字以内。")
    if find(new)[0]:
        raise SystemExit(f"已经有一个叫「{new}」的微信号了。")
    registry.update(hub, bot_name=new)


def add_project(hub, path, **settings):
    e = registry.get(hub)
    projects = dict(e.get("projects") or {})
    projects[os.path.realpath(path)] = {**(projects.get(os.path.realpath(path)) or {}),
                                        **{k: v for k, v in settings.items() if v}}
    registry.update(hub, projects=projects)


def remove_project(hub, path):
    e = registry.get(hub)
    projects = dict(e.get("projects") or {})
    projects.pop(os.path.realpath(path), None)
    registry.update(hub, projects=projects)


def ensure_running(hub):
    """没在跑就常驻起来；在跑就不用管，新加的项目它自己会读到。"""
    r = daemon.running(hub)
    if r:
        return r[0]
    return daemon.start_always(hub, "weixin")


def migrate():
    """0.9.0 的微信是「一个项目一个微信机器人」，改成微信号管项目：登录信息、会话记录都搬过去，不用重新扫码。"""
    for path, e in list(registry.all_projects().items()):
        if e.get("channel") != "weixin" or e.get("kind"):
            continue
        r = daemon.running(path)
        always = daemon.is_always(path)
        daemon.stop(path)
        hub = hub_path(_new_name())
        os.makedirs(hub, exist_ok=True)
        entry = {k: v for k, v in e.items() if k not in SETTINGS + ("runtime", "stable_version", "group", "reaction")}
        entry.update(kind="weixin", bot_name=os.path.basename(hub),
                     projects={path: {k: e[k] for k in SETTINGS if e.get(k)}})
        for k in ("runtime", "stable_version"):
            if e.get(k):
                entry[k] = e[k]
        registry.remove(path)
        registry.put(hub, entry)
        old = registry.read_state(path)
        registry.write_state(hub, chats={path: old.get("chats") or {}})
        registry.write_state(path, chats={})
        src = os.path.join(registry.pdir(path), "weixin.json")
        if os.path.exists(src):
            shutil.move(src, os.path.join(registry.pdir(hub), "weixin.json"))
        print(f"微信改成了一个微信号管多个项目：「{os.path.basename(path)}」现在由微信「{entry['bot_name']}」负责。", flush=True)
        if r or always:
            ensure_running(hub)


# ---------- 运行：一个微信号的进程 ----------

class HubStore:
    """一个项目在这个微信号里的会话记录。"""

    def __init__(self, hub, path):
        self.hub, self.path = hub, path

    def read(self):
        return (registry.read_state(self.hub).get("chats") or {}).get(self.path) or {}

    def write(self, chats):
        allc = registry.read_state(self.hub).get("chats") or {}
        allc[self.path] = chats
        registry.write_state(self.hub, chats=allc)


class ProjectChannel:
    """给一个项目用的微信：转给真正的微信渠道；不是当前项目时，消息前面加「【项目名】」，也不显示「正在输入」。"""

    def __init__(self, hub, path):
        self.hub, self.path, self.name = hub, path, os.path.basename(path)

    def __getattr__(self, k):
        return getattr(self.hub.channel, k)

    def _tag(self, text):
        return text if self.hub.current == self.path else f"【{self.name}】\n{text}"

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        await self.hub.channel.send_text(chat_id, self._tag(markdown or "（空）"), reply_to, uid)

    async def send_card(self, chat_id, card, reply_to=None):
        await self.hub.channel.send_text(chat_id, self._tag(self.hub.channel._render(card)))

    async def typing(self, chat_id, on):
        if self.hub.current == self.path:
            await self.hub.channel.typing(chat_id, on)


class Hub:
    """一个微信号的进程：认主人、/项目 切换、审批的 y / n 答给在等的那个项目，其余交给当前项目的 Bridge。"""

    def __init__(self, home, entry):
        self.home, self.entry = home, entry
        self.channel = None
        self.bridges = {}
        self.current = registry.read_state(home).get("current")
        self.picking = {}                     # 聊天 → 发出项目列表的时间

    def handlers(self):
        async def noop(*a):
            return None, None
        return {"on_message": self.on_message, "on_action": noop, "on_bot_added": noop}

    def attach(self, channel):
        self.channel = channel

    def projects(self):
        """每次现读：ccim --weixin 加的项目不用重启就能用。"""
        return list(((registry.get(self.home) or {}).get("projects") or {}).items())

    def _entry(self, path, settings):
        proj = registry.get(path) or {}               # 同时接了飞书的项目：模型、助手等设置跟项目走
        e = {k: proj[k] for k in SETTINGS if proj.get(k)}
        e.update({k: v for k, v in (settings or {}).items() if v})
        e.update(channel="weixin", owner_open_id=self.entry.get("owner_open_id"))
        return e

    def bridge(self, path, settings=None):
        from .bridge import Bridge
        b = self.bridges.get(path)
        if not b:
            b = self.bridges[path] = Bridge(path, self._entry(path, settings), store=HubStore(self.home, path), home=self.home)
            b.attach(ProjectChannel(self, path))
            b.help_extra = "\n\n/项目　看看有哪些项目、当前是哪个\n/项目 名字　切到那个项目（也可以写序号）"
        return b

    def _set_current(self, path):
        self.current = path
        registry.write_state(self.home, current=path)

    async def on_message(self, inc):
        if inc.sender_id != self.entry.get("owner_open_id"):
            log.info("忽略 %s 的消息（不是主人）", inc.sender_id)
            return
        projects = self.projects()
        for p in [p for p in self.bridges if p not in dict(projects)]:   # 被 ccim weixin remove 移出去的
            await self.bridges.pop(p).shutdown()
        if not projects:
            await self.channel.send_text(inc.chat_id, "还没有项目。在电脑上进到项目目录，运行 ccim --weixin 把它加进来。")
            return
        text = (inc.text or "").strip()
        if text and not inc.files:                    # 审批的 y / n：哪个项目在等就答给哪个
            for b in list(self.bridges.values()):
                if b.approvals.has_pending(inc.chat_id) and await b.approvals.answer_by_text(inc.chat_id, text):
                    return
        cmd, _, arg = text.partition(" ")
        if cmd.lower() in SWITCH and not inc.files:
            await self._switch(inc.chat_id, arg.strip(), projects)
            return
        shown = self.picking.pop(inc.chat_id, 0)
        if shown and time.monotonic() - shown < PICK_WINDOW and not inc.files and self._pick(text, projects, exact=True):
            await self._switch(inc.chat_id, text, projects)   # 刚看了项目列表，直接回了序号或项目名
            return
        if self.current not in dict(projects):
            self._set_current(projects[0][0])
        await self.bridge(self.current, dict(projects)[self.current]).on_message(inc)

    @staticmethod
    def _pick(arg, projects, exact=False):
        """按序号或名字找项目。exact：只认序号和完整的项目名（用户回的可能是别的话）。"""
        arg = arg.strip()
        if arg.isdigit():
            return [projects[int(arg) - 1]] if 0 < int(arg) <= len(projects) else []
        hits = [x for x in projects if os.path.basename(x[0]) == arg]
        return hits if hits or exact else [x for x in projects if arg.lower() in os.path.basename(x[0]).lower()]

    def _busy(self, path):
        b = self.bridges.get(path)
        return bool(b and any(c.busy for c in b.chats.values()))

    async def _switch(self, chat_id, arg, projects):
        say = lambda s: self.channel.send_text(chat_id, s)
        if not arg:
            if self.current not in dict(projects):
                self._set_current(projects[0][0])
            self.picking[chat_id] = time.monotonic()
            lines = ["项目（回复序号切换）："]
            for i, (p, _) in enumerate(projects, 1):
                tags = [t for t, on in (("当前", p == self.current), ("干活中", self._busy(p))) if on]
                lines.append(f"{i}. {os.path.basename(p)}" + (f"（{'，'.join(tags)}）" if tags else ""))
            await say("\n".join(lines))
            return
        hits = self._pick(arg, projects)
        if len(hits) != 1:
            await say(("有好几个项目都对得上：" if hits else "没有这个项目。现在有：") +
                      "、".join(os.path.basename(p) for p, _ in (hits or projects)))
            return
        path = hits[0][0]
        self._set_current(path)
        busy = "它还在干活，做完会把结果发过来。" if self._busy(path) else "接着说就行。"
        await say(f"已切到「{os.path.basename(path)}」，{busy}")

    async def shutdown(self):
        for b in list(self.bridges.values()):
            await b.shutdown()
