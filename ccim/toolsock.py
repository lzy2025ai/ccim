"""把 ccim 给助手的工具开放给任意底座（#178）。

Claude Code 的工具跑在 ccim 进程里（SDK 的进程内 MCP）。别的底座（Codex，以后接的）是单独的进程，拿不到，
所以每个 ccim 进程再开一个只有本用户能连的 unix socket（~/.ccim/run/tools-<pid>.sock）：
会话启动时发一个令牌（环境变量 CCIM_TOKEN，socket 地址在 CCIM_TOOLS），凭令牌找到是哪个对话，
调的就是这个对话自己那份工具（Chat._tool_list），所以权限、审批和 Claude Code 里完全一样。

外面怎么连：`ccim/mcpstdio.py` 是标准 MCP 服务（stdio），Codex 启动时挂上；不支持 MCP 的底座用 `ccim tool` 命令。
协议：一行 JSON 请求 {token, op: list | call, name, args}，一行 JSON 回应。
"""
import asyncio, json, logging, os, secrets, weakref

from . import registry

log = logging.getLogger("ccim.toolsock")

_chats = weakref.WeakValueDictionary()        # 令牌 → Chat
_server = None
_lock = None


def path():
    return os.path.join(registry.HOME, "run", f"tools-{os.getpid()}.sock")


def token(chat):
    t = getattr(chat, "_tool_token", None)
    if not t:
        t = chat._tool_token = secrets.token_urlsafe(24)
    _chats[t] = chat
    return t


def _schema(t):
    try:
        from claude_agent_sdk import _build_input_schema
        return _build_input_schema(t)
    except ImportError:
        s = t.input_schema
        return s if isinstance(s, dict) and "properties" in s else {"type": "object", "properties": {}}


def _sweep(d):
    """上次没收拾的 socket（进程已经不在了）。"""
    for f in os.listdir(d):
        if f.startswith("tools-") and f.endswith(".sock"):
            try:
                pid = int(f[6:-5])
                if pid != os.getpid():
                    os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
            except (ValueError, OSError):
                pass


async def start():
    """在主循环里开 socket（开过就直接返回）。会话启动前调。"""
    global _server, _lock
    if _server:
        return
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if _server:
            return
        p = path()
        d = os.path.dirname(p)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        _sweep(d)
        if os.path.exists(p):
            os.remove(p)
        _server = await asyncio.start_unix_server(_serve, path=p, limit=64 * 1024 * 1024)
        os.chmod(p, 0o600)
        import atexit
        atexit.register(lambda: os.path.exists(p) and os.remove(p))


async def _serve(reader, writer):
    try:
        line = await reader.readline()
        try:
            req = json.loads(line)
            out = await _handle(req)
        except Exception as e:
            log.warning("工具调用出错", exc_info=True)
            out = {"error": f"出错了：{e}"}
        writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _handle(req):
    chat = _chats.get(req.get("token") or "")
    if not chat:
        return {"error": "这个会话已经结束了，工具用不了。"}
    tools = {t.name: t for t in chat._tool_list()}
    if req.get("op") == "list":
        return {"tools": [{"name": t.name, "description": t.description, "inputSchema": _schema(t)} for t in tools.values()]}
    t = tools.get(req.get("name") or "")
    if not t:
        return {"error": f"没有 {req.get('name')} 这个工具。"}
    res = await t.handler(req.get("args") or {})
    return {"result": res}
