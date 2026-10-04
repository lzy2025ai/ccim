"""用 Codex 代替 Claude Code：每个聊天对应一个 Codex 对话（thread）。

一轮 = 跑一次 `codex exec --json`（接着聊就是 `codex exec ... resume <thread_id> <消息>`），读它逐行吐出的事件：
thread.started（对话编号）→ turn.started → item.started / item.completed（命令、改文件、回复……）→ turn.completed / turn.failed。
标准输入必须关掉，否则它一直等输入。

和 Claude Code 的不同：
- 正在跑的 exec 不接受中途补充（`codex queue` 放进去它也看不到），任务进行中再来的消息排到这一轮之后。
- 没有 send_file 工具，改成约定：回复里单独一行写「[发送文件] 路径」，ccim 发出去并从回复里去掉。
- 模型、思考深度、沙箱、审批跟随 Codex 自己的设置（~/.codex/config.toml、项目 .codex/config.toml），ccim 只在用户单独设置时传。
"""
import asyncio, glob, json, logging, os, re, signal, time, tomllib

from .progress import short
from .session import Chat, Turn

log = logging.getLogger("ccim.codex")

EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
LINE_LIMIT = 64 * 1024 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
NOTE = """\
（以下是 ccim 的说明，不用回复这一段）你正在通过飞书和用户对话，用户多半在手机上看回复：
- 回答简洁，用飞书能显示的 Markdown（标题、加粗、列表、代码块、链接），不要用表格和 HTML。
- 需要把图片、视频、文件交给用户时，在回复里单独一行写：[发送文件] 文件的绝对路径（一行一个），ccim 会把它发到聊天里。
- 用户发来的图片和文件已保存到本地，路径写在消息里。
- 没有交互式提问框，有问题直接在回复里问。

"""
RECONNECT = "网络不稳，重连中"
SEND_RE = re.compile(r"^\s*\[发送文件\]\s*(.+?)\s*$", re.M)


def find_codex():
    """ChatGPT 桌面版自带的 codex 不依赖 Node，后台进程里也能跑；没有再找 nvm 装的（直接用里面的原生程序，不经过 node）。"""
    app = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    if os.access(app, os.X_OK):
        return app
    hits = sorted(glob.glob(os.path.expanduser(
        "~/.nvm/versions/node/*/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-*/vendor/*/bin/codex")))
    return hits[-1] if hits else None


def defaults(path):
    """Codex 自己设置里的模型和思考深度：项目 .codex/config.toml 优先，其次 ~/.codex/config.toml。"""
    model = effort = None
    for f in (os.path.join(path, ".codex", "config.toml"), os.path.expanduser("~/.codex/config.toml")):
        try:
            with open(f, "rb") as fh:
                c = tomllib.load(fh)
        except (OSError, ValueError):
            continue
        model = model or c.get("model")
        effort = effort or c.get("model_reasoning_effort")
    return model, effort


def _command(cmd):
    """`/bin/zsh -lc 'ls -la'` → `ls -la`。"""
    if isinstance(cmd, list):
        cmd = " ".join(cmd)
    m = re.match(r"^\S*/(?:ba|z)?sh -l?c (.*)$", cmd or "", re.S)
    if not m:
        return cmd or ""
    inner = m.group(1).strip()
    if len(inner) > 1 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner


def describe(item):
    """Codex 的一步，写成工作中卡片上的一行；返回 None 表示不显示。"""
    kind = item.get("type")
    if kind == "command_execution":
        return "运行：" + short(_command(item.get("command")), 80)
    if kind == "file_change":
        names = [os.path.basename(c.get("path", "")) for c in item.get("changes") or []]
        return "修改 " + "、".join(n for n in names if n)[:80] if names else "修改文件"
    if kind == "mcp_tool_call":
        return "调用工具 " + str(item.get("tool") or "")
    if kind == "web_search":
        return "搜索：" + short(item.get("query"), 50)
    return None


class CodexChat(Chat):
    agent = "codex"
    SID_KEY = "codex_session_id"

    def __init__(self, bridge, chat_id, chat_type, fork_from=None):
        super().__init__(bridge, chat_id, chat_type, None)   # Codex 不支持从别的对话分支出来
        self.fork_from = self.fork_at = None
        self.proc = None

    # ----- 和 Claude Code 不同的几处 -----

    async def inject(self, prompt, reply_to=None):
        return False                          # 正在跑的 exec 不接受补充，由调用方排到这一轮之后

    async def _interrupt(self):
        if not self.turn:
            return False
        self.stopping = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        return True

    async def _connect(self):
        pass

    async def _disconnect(self):
        pass

    def effective(self):
        m, e = defaults(self.b.path)
        return self.model or m or "默认", self.effort or e or "默认", bool(self.model), bool(self.effort)

    # ----- 一轮 -----

    async def _turn_inner(self, prompt, reply_to):
        t = self.turn = Turn()
        ticker = asyncio.create_task(self._tick(t, reply_to))
        result, error, last_error = None, None, None
        try:
            exe = find_codex()
            if not exe:
                raise RuntimeError("这台电脑上找不到 Codex，先安装 ChatGPT 桌面版或 Codex 命令行")
            args = [exe, "exec", "--json", "--skip-git-repo-check", "-C", self.b.path]
            if self.model:
                args += ["-m", self.model]
            if self.effort:
                args += ["-c", f"model_reasoning_effort={self.effort}"]
            for p in re.findall(r"^(/\S+)$", prompt, re.M):   # 用户发来的图片直接给 Codex 看
                if p.lower().endswith(IMAGE_EXT) and os.path.isfile(p):
                    args += ["-i", p]
            if self.session_id:
                args += ["resume", self.session_id, prompt]
            else:
                args.append(NOTE + prompt)
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.b.path, limit=LINE_LIMIT, start_new_session=True)
            t.ready = time.monotonic()
            err_task = asyncio.create_task(self.proc.stderr.read())
            async for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind, item = ev.get("type"), ev.get("item") or {}
                if kind == "thread.started" and ev.get("thread_id"):
                    if ev["thread_id"] != self.session_id:
                        self.session_id = ev["thread_id"]
                        self.b.save(self.chat_id, **{self.SID_KEY: self.session_id})
                elif kind == "item.started":
                    d = describe(item)
                    if d:
                        t.steps.append(d)
                elif kind == "item.completed":
                    if item.get("type") == "agent_message" and (item.get("text") or "").strip():
                        t.text = item["text"]
                    elif item.get("type") == "file_change":   # 改文件只在完成时报
                        d = describe(item)
                        if d:
                            t.steps.append(d)
                elif kind == "turn.completed":
                    result = t.text
                elif kind == "error":
                    # 不一定是失败：网络断了 Codex 会自己重连（「Reconnecting... 2/5」），之后照样回答。先记下，最后没有回答才算出错
                    last_error = ev.get("message") or last_error
                    if "Reconnecting" in (ev.get("message") or "") and RECONNECT not in t.steps:
                        t.steps.append(RECONNECT)
                elif kind == "turn.failed":
                    msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
                    if not self.stopping:
                        error = msg or last_error or "Codex 出错了"
            await self.proc.wait()
            stderr = (await err_task).decode("utf-8", "replace")
            if not result and t.text and not error:
                result = t.text                   # 没等到 turn.completed 也有回答：照发
            if not result and not error and not self.stopping and last_error:
                error = last_error
            if self.proc.returncode not in (0, None) and not result and not error and not self.stopping:
                lines = [l for l in stderr.splitlines() if l.strip() and "failed to refresh available models" not in l]
                error = short(lines[-1] if lines else f"Codex 异常退出（{self.proc.returncode}）", 300)
        finally:
            ticker.cancel()
            self.proc = None
        if result and not error:
            t.steps = [s for s in t.steps if s != RECONNECT]   # 已经回答了，重连只是过程，不留在卡片上
        if result:
            result = await self._send_files(result)
        await self._finish(t, result, error, reply_to)

    async def _send_files(self, text):
        """回复里「[发送文件] 路径」那几行：把文件发出去，再从回复里去掉。"""
        root = os.path.realpath(self.b.path)
        for raw in SEND_RE.findall(text):
            path = os.path.realpath(os.path.join(self.b.path, os.path.expanduser(raw.strip("`'\""))))
            if self.chat_type != "p2p" and not path.startswith(root + os.sep):
                await self._safe_text(f"群聊里只能发送项目目录里的文件：{os.path.basename(path)}")
            elif not os.path.isfile(path):
                await self._safe_text(f"文件不存在：{path}")
            else:
                try:
                    await self.b.channel.send_file(self.chat_id, path)
                except Exception as e:
                    await self._safe_text(f"{os.path.basename(path)} 没发出去：{e}")
        return SEND_RE.sub("", text).strip()
