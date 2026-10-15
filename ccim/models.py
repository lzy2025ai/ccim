"""能选的模型：和 Claude Code 的 /model、Codex 的模型列表一致。

Claude Code：连一次会话拿 initialize 里的 models（不发消息，不花额度），存 ~/.ccim/models.json，一天刷新一次。
要用和对话同一个 claude（session.CLI）：不同版本的 claude 里 sonnet、haiku 指向的型号不一样。
「更多模型」（旧版本）：Claude Code 按账号缓存的模型目录 ~/.claude/cache/model-catalog/*-cc.json 里 section=overflow 的，
和桌面端模型菜单里的「更多模型」一致。
Codex：读它自己缓存的 ~/.codex/models_cache.json（只要 visibility=list 的）。
"""
import asyncio, glob, json, logging, os, time

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
    if out:                                   # 账号模型目录里有、这个版本的 claude 别名没指到的（比如更新的 Sonnet），也列出来
        cat = _catalog()
        older = {x["id"] for x in cat if x.get("section") != "main"}
        out = [o + ["more"] if o[3] in older else o for o in out]   # 别名指到的是旧型号：放进「更多模型」
        have = {o[3] for o in out}
        for x in cat:
            if x["id"] not in have:
                have.add(x["id"])
                fam = _family({"value": x["id"]})   # 同系列已经有新的了（比如 haiku 指到 5.5），旧的放进「更多模型」
                more = x.get("section") != "main" or any(len(o) < 5 and _family({"value": o[3]}) == fam for o in out)
                out.append([x["id"], x.get("name") or x["id"], "" if more else DESC.get(_family({"value": x["id"]}), ""),
                            x["id"]] + (["more"] if more else []))
        # 和桌面端一样：Opus、Fable、Sonnet、Haiku；「更多模型」在后面，按账号模型目录里的顺序
        fam, pos = list(DESC), {x["id"]: i for i, x in enumerate(cat)}
        out = [o[:2] + [""] + o[3:] if len(o) > 4 else o for o in out]
        out.sort(key=lambda o: (1, pos.get(o[3], 99)) if len(o) > 4 else
                 (0, fam.index(_family({"value": o[3]})) if _family({"value": o[3]}) else 9))
    return out


def _catalog():
    files = sorted(glob.glob(os.path.expanduser("~/.claude/cache/model-catalog/*-cc.json")), key=os.path.getmtime, reverse=True)
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                ms = ((json.load(fh).get("catalog") or {}).get("config") or {}).get("models") or []
        except (OSError, ValueError, AttributeError):
            continue
        return [m for m in ms if m.get("id") and m.get("section") in ("main", "overflow")]
    return []


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
    from .session import CLI
    client = ClaudeSDKClient(ClaudeAgentOptions(cwd=os.path.expanduser("~"), cli_path=CLI))
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
