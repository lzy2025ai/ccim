"""/usage：Claude 订阅的 5 小时额度、每周额度和用量明细，排成中文。

额度：每轮对话里 SDK 会推 RateLimitEvent（unifiedWindows 里有 five_hour、seven_day 的已用比例和重置时间），
记到 ~/.ccim/rate.json；/usage 的原始输出里也有 Current session / Current week 两行，新开的会话有时还没拿到就没有。
明细：用一个临时会话跑 Claude Code 自带的 /usage，按已知的句式翻成中文，认不出的原样保留。
额度要在会话里发过请求才有：缓存超过 10 分钟就先用 haiku 问一句最短的话再跑 /usage。
"""
import json, os, re, time

from . import registry

WINDOWS = {"five_hour": "5 小时额度", "seven_day": "每周额度", "seven_day_opus": "每周额度（Opus）",
           "seven_day_sonnet": "每周额度（Sonnet）"}


def _file():
    return os.path.join(registry.HOME, "rate.json")


def record(info):
    """session 里收到 RateLimitEvent 时调用。"""
    raw = getattr(info, "raw", None) or {}
    wins = raw.get("unifiedWindows")
    if not wins:
        return
    data = {"windows": wins, "at": time.time()}
    tmp = _file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _file())


def cached():
    try:
        with open(_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _when(ts):
    t = time.localtime(ts)
    today = time.localtime()
    day = "今天" if t[:3] == today[:3] else time.strftime("%m-%d", t)
    return f"{day} {time.strftime('%H:%M', t)}"


def limit_lines(data=None):
    """「5 小时额度：已用 5%，今天 19:40 重置」这样的几行；没有数据返回 []。"""
    data = data or cached()
    if not data:
        return []
    out = []
    for key, name in WINDOWS.items():
        w = (data.get("windows") or {}).get(key)
        if not w or w.get("utilization") is None:
            continue
        line = f"**{name}**：已用 {round(w['utilization'] * 100)}%"
        if w.get("resetsAt"):
            line += f"，{_when(w['resetsAt'])} 重置"
        out.append(line)
    return out


def short_status():
    """给 /status 用的一行：「5 小时已用 5% · 每周已用 14%」。"""
    data = cached()
    if not data:
        return None
    w = data.get("windows") or {}
    parts = [f"{label}已用 {round(w[k]['utilization'] * 100)}%" for k, label in
             (("five_hour", "5 小时"), ("seven_day", "每周")) if (w.get(k) or {}).get("utilization") is not None]
    return " · ".join(parts) or None


# ---------- 明细翻译 ----------

_REASONS = [
    (r"was at >(\d+)k context", lambda m: f"发生在上下文超过 {int(m.group(1)) // 10} 万 token 时"),
    (r"came from sessions active for (\d+)\+ hours", lambda m: f"来自持续 {m.group(1)} 小时以上的会话"),
    (r"came from subagent-heavy sessions", lambda m: "来自大量使用子任务的会话"),
    (r"was while (\d+)\+ sessions ran in parallel", lambda m: f"发生在同时开着 {m.group(1)} 个以上会话时"),
]
_TOPS = {"Top skills": "用得最多的 skill", "Top subagents": "用得最多的子任务", "Top plugins": "用得最多的插件",
         "Top MCP servers": "用得最多的 MCP 服务", "Top tools": "用得最多的工具"}
_SKIP = ("You are currently using", "What's contributing", "Approximate, based on", "Current session", "Current week")


def translate(raw):
    """把 Claude Code /usage 的明细部分翻成中文。"""
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith(_SKIP):
            continue
        m = re.match(r"Last (24h|7d|\d+d) · ([\d,]+) requests · ([\d,]+) sessions", s)
        if m:
            span = {"24h": "最近 24 小时", "7d": "最近 7 天"}.get(m.group(1), "最近 " + m.group(1))
            out += ["", f"**{span}**：{m.group(2)} 次请求，{m.group(3)} 个会话"]
            continue
        m = re.match(r"(\d+)% of your usage (.+)", s)
        if m:
            for pat, fn in _REASONS:
                mm = re.fullmatch(pat, m.group(2))
                if mm:
                    out.append(f"· {m.group(1)}% {fn(mm)}")
                    break
            else:
                out.append(f"· {s}")
            continue
        m = re.match(r"(Top [A-Za-z ]+): (.+)", s)
        if m and m.group(1) in _TOPS:
            out.append(f"· {_TOPS[m.group(1)]}：{re.sub(r'(^|, )/', r'\1', m.group(2))}")
            continue
        out.append(s)
    return out


def _limits_from_raw(raw):
    """原始输出里的 Current session / Current week 两行（没有 rate.json 时兜底）。"""
    out = []
    for en, name in (("Current session", "5 小时额度"), ("Current week (all models)", "每周额度")):
        m = re.search(re.escape(en) + r": (\d+)% used(?: · resets (.+?))?(?: \(|$)", raw or "", re.M)
        if m:
            out.append(f"**{name}**：已用 {m.group(1)}%" + (f"，{m.group(2)} 重置" if m.group(2) else ""))
    return out


async def report(path):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent, ResultMessage
    from .session import CLI
    raw = ""
    data = cached()
    stale = not data or time.time() - data.get("at", 0) > 600
    try:
        opts = ClaudeAgentOptions(cwd=path, cli_path=CLI, setting_sources=[], max_turns=1, model="haiku")
        async with ClaudeSDKClient(options=opts) as c:
            if stale:                         # 额度要发过请求才拿得到：用最便宜的模型问一句最短的
                await c.query("只回答：好")
                async for m in c.receive_response():
                    if isinstance(m, RateLimitEvent):
                        record(m.rate_limit_info)
            await c.query("/usage")
            async for m in c.receive_response():
                if isinstance(m, ResultMessage):
                    raw = m.result or ""
    except Exception:
        pass
    data = cached()
    fresh = data and time.time() - data.get("at", 0) < 600
    limits = (limit_lines(data) if fresh else []) or _limits_from_raw(raw) or limit_lines(data)
    lines = limits or ["额度暂时查不到，聊一句之后再发 /usage 试试。"]
    detail = translate(raw)
    if detail:
        lines += detail + ["", "明细只统计这台电脑上的使用，不含其他设备和网页版。"]
    return "\n".join(lines)
