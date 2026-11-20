"""这台电脑上各个底座装没装、能不能用（#522）：网页「设置 → 电脑」里标出来，没装底座的电脑也能接入。

只看文件，不启动底座（整体状态常要算，问一次 claude auth status 要一两秒）：
- Claude Code：本机的 claude 或 SDK 自带的；登录看 ~/.claude.json 里的账号、~/.claude/.credentials.json，或者配了 API Key
- Codex：codex.find_codex()；登录看 ~/.codex/auth.json 里有没有令牌或 API Key
- DeepSeek Harness：dsh.status()
"""
import json, os

ORDER = ("claude", "codex", "dsh")
_cache = {}                                   # 文件 → (修改时间, 结果)


def _read_json(path, pick):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            v = pick(json.load(f))
    except (OSError, ValueError, AttributeError, TypeError):
        v = None
    _cache[path] = (mt, v)
    return v


def _claude_cli():
    from .session import CLI
    if CLI:
        return CLI
    try:
        import claude_agent_sdk
        d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
        return next((os.path.join(d, n) for n in ("claude", "claude.exe") if os.path.exists(os.path.join(d, n))), None)
    except ImportError:
        return None


def _claude_login():
    home = os.path.expanduser("~")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if _read_json(os.path.join(home, ".claude.json"), lambda d: bool(d.get("oauthAccount"))):
        return True
    if os.path.exists(os.path.join(home, ".claude", ".credentials.json")):
        return True
    env = _read_json(os.path.join(home, ".claude", "settings.json"), lambda d: d.get("env") or {}) or {}
    helper = _read_json(os.path.join(home, ".claude", "settings.json"), lambda d: bool(d.get("apiKeyHelper")))
    return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or helper)


def _codex_login():
    if os.environ.get("OPENAI_API_KEY"):
        return True
    return bool(_read_json(os.path.expanduser("~/.codex/auth.json"),
                           lambda d: bool(d.get("tokens") or d.get("OPENAI_API_KEY"))))


def status():
    """{底座: {installed, ready, need}}。need：没就绪时差什么（install 没装 / login 没登录 / key 没填 Key）。"""
    from .codex import find_codex
    from . import dsh
    out = {}
    cl = bool(_claude_cli())
    out["claude"] = _row(cl, cl and _claude_login(), "login")
    cx = bool(find_codex())
    out["codex"] = _row(cx, cx and _codex_login(), "login")
    d = dsh.status()
    out["dsh"] = _row(d["ready"], d["ready"] and d["key"], "key")
    return out


def _row(installed, ready, missing):
    return {"installed": bool(installed), "ready": bool(ready),
            "need": None if ready else ("install" if not installed else missing)}
