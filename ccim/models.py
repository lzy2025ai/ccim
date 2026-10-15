"""能选的模型：和 Claude Code 的 /model、Codex 的模型列表一致。

Claude Code：连一次会话拿 initialize 里的 models（不发消息，不花额度），存 ~/.ccim/models.json，一天刷新一次。
Codex：读它自己缓存的 ~/.codex/models_cache.json（只要 visibility=list 的）。
"""
import asyncio, json, logging, os, time

from . import registry

log = logging.getLogger("ccim.models")

TTL = 24 * 3600
DESC = {"opus": "复杂任务的首选", "fable": "最强，适合最难、最久的任务", "sonnet": "快，日常够用", "haiku": "最快最省"}


def _file():
    return os.path.join(registry.HOME, "models.json")


def _family(m):
    for k in DESC:
        if k in (m.get("value") or "") or k in (m.get("resolvedModel") or ""):
            return k
    return ""


def claude():
    """[[值, 名字, 说明]]；还没取到时返回空，网页用自带的几个系列。"""
    try:
        with open(_file(), encoding="utf-8") as f:
            raw = json.load(f).get("claude") or []
    except (OSError, ValueError):
        return []
    out = []
    for m in raw:
        if m.get("value") == "default":
            continue
        name = (m.get("description") or "").split("·")[0].strip() or m.get("displayName") or m["value"]
        out.append([m["value"], name, DESC.get(_family(m), ""), m.get("resolvedModel") or ""])
    return out


def codex():
    try:
        with open(os.path.expanduser("~/.codex/models_cache.json"), encoding="utf-8") as f:
            raw = json.load(f).get("models") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [[m["slug"], m.get("display_name") or m["slug"], "", m["slug"]] for m in raw
            if m.get("slug") and m.get("visibility", "list") == "list"]


def stale():
    try:
        return time.time() - os.path.getmtime(_file()) > TTL
    except OSError:
        return True


async def refresh():
    """问 Claude Code 现在能用哪些模型。失败就留着旧的。"""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    client = ClaudeSDKClient(ClaudeAgentOptions(cwd=os.path.expanduser("~")))
    try:
        await asyncio.wait_for(client.connect(), 60)
        info = await client.get_server_info() or {}
        if info.get("models"):
            tmp = _file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"claude": info["models"], "at": time.time()}, f, ensure_ascii=False)
            os.replace(tmp, _file())
    except Exception:
        log.warning("取模型列表失败", exc_info=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
