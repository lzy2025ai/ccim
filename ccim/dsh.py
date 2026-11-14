"""用 DeepSeek Harness（dsh）当底座：每个聊天对应一个 dsh 对话（session），#424。

一轮 = 跑一次 `dsh --profile headless --json -`，任务从标准输入给（消息以 - 开头也不会被当成参数），接着聊加 --session-id。
读它逐行吐出的事件：session → status → thinking / text / tool_call / tool_result → final，出错是 error。

和 Codex 的不同：
- --patch 是启动器的参数，必须写在 --json 前面，写在后面 headless 不认（unknown option）。
- 找 dsh 只认 ~/.ccim/dsh 里装的，不从 PATH 找：PyPI 上有个无关的项目命令也叫 dsh。
- 它自己的记录是压缩的内部格式，版本一变就可能读不了，所以 ccim 自己记一份（history.dsh_file），网页的历史读这份。
- 权限：工作区可写（dsh 的默认），侧边对话只读。无人值守时越界的写入被沙箱拒掉，不会卡住等审批；ccim 的工具不走审批。
- 访客不能用；模型、思考深度第一版不传，用 dsh 的默认。不支持分叉：fork_from 被忽略，开新对话。
- MCP 子进程拿不到环境里带凭据字样的变量，令牌要在 patch 的 env 里写明；Key 只经环境变量给 dsh，工具的环境里看不到。
"""
import asyncio, json, logging, os, shutil, time

from .codex import NOTE, TOOL_WAIT, CodexChat
from .progress import describe_tool, short
from . import devices, registry
from .session import SKILL_SHARE, Turn, _can_sync, _tools_ready, prompt_note, role_note

log = logging.getLogger("ccim.dsh")

LINE_LIMIT = 64 * 1024 * 1024
ROOT = os.path.join(registry.HOME, "dsh")
BIN = os.path.join(ROOT, "pkg", "node_modules", ".bin", "dsh")
KEY = os.path.join(ROOT, "api-key")


def find_node():
    """后台进程的 PATH 可能很短，常见的位置也找一遍。"""
    return shutil.which("node") or next((p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if os.access(p, os.X_OK)), None)


def find_dsh():
    return BIN if os.access(BIN, os.X_OK) and find_node() else None


def read_key():
    try:
        with open(KEY, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_key(key):
    os.makedirs(ROOT, exist_ok=True)
    fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key.strip())
    os.chmod(KEY, 0o600)


def status():
    return {"ready": bool(find_dsh()), "key": bool(read_key())}


def patch_file():
    """把 ccim 的工具作为 MCP 服务挂上。内容里没有令牌，令牌每轮经环境变量传，所以一个文件所有对话共用。"""
    import sys
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcpstdio.py")
    q = json.dumps
    text = f"""- insert:
    - id: ccim-tools
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ccim
        transport: stdio
        command: {q(sys.executable)}
        args: {q(["-I", script])}
        toolCallTimeoutMs: {TOOL_WAIT * 1000}
        env:
          CCIM_TOKEN: !!js process.env.CCIM_TOKEN
          CCIM_TOOLS: !!js process.env.CCIM_TOOLS
"""
    path = os.path.join(ROOT, "ccim-tools.yml")
    os.makedirs(ROOT, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def describe(tool, inp):
    """dsh 的一步，写成工作中卡片上的一行。"""
    inp = inp or {}
    if tool == "bash":
        return "运行：" + short(inp.get("command"), 80)
    if (tool or "").startswith("mcp__"):
        return describe_tool(tool, inp)
    return f"使用 {tool}" if tool else None


def record(session_id, rows):
    """这一轮记进 ccim 自己的记录（history.dsh 读）。"""
    d = os.path.join(ROOT, "history")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, session_id + ".jsonl"), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class DshChat(CodexChat):
    agent = "dsh"
    SID_KEY = "dsh_session_id"

    def effective(self):
        return "默认", "默认", False, False

    async def _turn_inner(self, prompt, reply_to):
        t = self.turn = Turn()
        ticker = asyncio.create_task(self._tick(t, reply_to))
        result, error, asked = None, None, time.time()
        try:
            exe, key = find_dsh(), read_key()
            if not exe:
                raise RuntimeError("这台电脑上还没装 DeepSeek Harness")
            if not key:
                raise RuntimeError("还没填 DeepSeek API Key，去「设置」里填好再用")
            if self._guest():                 # 按这一轮说话的人算（#419）
                raise RuntimeError("这台电脑现在只能用 Claude Code，去 Agent 设置里把底座换成 Claude Code")
            await _tools_ready()
            env = self.ccim_env()
            node = find_node()
            run = {**os.environ, **env, "DSH_HOME": os.path.join(ROOT, "home"), "DEEPSEEK_API_KEY": key,
                   "PATH": os.path.dirname(node) + os.pathsep + env["PATH"]}
            run.pop("DSH_PERMISSION_MODE", None)
            args = [node, os.path.realpath(exe), "--profile", "headless", "--patch", patch_file(), "--json"]
            text = prompt
            if self.session_id:
                args += ["--session-id", self.session_id]
            else:
                note = role_note(self.b.entry)
                text = (prompt_note(NOTE, self.b.channel).rstrip("\n") + devices.perm_note() + (SKILL_SHARE if _can_sync(self) else "")
                        + "\n\n" + note.lstrip() + "\n\n" * bool(note) + prompt)
            self.proc = await asyncio.create_subprocess_exec(
                *args, "-", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.b.path, limit=LINE_LIMIT, start_new_session=True, env=run)
            t.ready = time.monotonic()
            err_task = asyncio.create_task(self.proc.stderr.read())
            self.proc.stdin.write(text.encode("utf-8"))
            await self.proc.stdin.drain()
            self.proc.stdin.close()
            async for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "session" and ev.get("sessionId"):
                    if ev["sessionId"] != self.session_id:
                        self.session_id = ev["sessionId"]
                        self.fork_from = None
                        self.b.save(self.chat_id, fork_from=None, **{self.SID_KEY: self.session_id})
                elif kind == "tool_call":
                    d = describe(ev.get("tool"), ev.get("input"))
                    if d:
                        t.steps.append(d)
                elif kind == "text" and (ev.get("text") or "").strip():
                    t.say(ev["text"])
                elif kind == "final":
                    result = ev.get("text") or t.text
                elif kind == "error" and not self.stopping:
                    error = ev.get("message") or "DeepSeek Harness 出错了"
            await self.proc.wait()
            stderr = (await err_task).decode("utf-8", "replace")
            if not result and t.text and not error:
                result = t.text               # 没等到 final 也有回答：照发
            if self.proc.returncode not in (0, None) and not result and not error and not self.stopping:
                lines = [l for l in stderr.splitlines() if l.strip()]
                error = short(lines[-1] if lines else f"DeepSeek Harness 异常退出（{self.proc.returncode}）", 300)
        finally:
            ticker.cancel()
            self.proc = None
        if self.session_id and (result or t.text):
            try:
                record(self.session_id, [{"type": "user", "text": prompt, "files": [], "at": asked},
                                         {"type": "answer", "text": result or t.text, "steps": list(t.steps), "state": "done", "at": time.time()}])
            except OSError as e:
                log.warning("记录没写上：%s", e)
        if result:
            result = await self._send_files(result)
        await self._finish(t, result, error, reply_to)
