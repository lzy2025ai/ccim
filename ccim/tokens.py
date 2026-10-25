"""Token 用量（#240）：从本机 Claude Code、Codex 的对话记录里按天汇总。

- Claude Code：~/.claude/projects 下每个会话一个 .jsonl（子 agent 在 <会话>/subagents/ 下，算到那个会话），
  回答那行的 message.usage。同一条消息会出现好几次（按内容块分成几行、分叉时原样复制到新会话），按 message.id 只算一次；
  同一个文件里取最后一行（前面几行的 output_tokens 不全）。分叉复制过去的消息算到原来那个会话：
  复制时连时间戳一起复制，所以按文件建立的时间认谁在先。
- Codex：~/.codex/sessions 下的 rollout-*.jsonl，每次请求一个 token_count 事件，取 last_token_usage。
- 「新产生的」= 输入 + 写入缓存 + 输出；读缓存另算（数量大、几乎不花额度）。
- 只读最近 KEEP_DAYS 天改过的文件；文件只往后追加，按读到的位置接着读。第一次全读一遍要十几秒，所以在后台线程里做。
"""
import datetime, glob, json, logging, os, threading, time

log = logging.getLogger("ccim.tokens")
KEEP_DAYS = 35
_FILES = {}           # 路径 → {"size", "off", "born", "sid", "msgs": {消息编号: (日期, 新产生, 读缓存)}}
_lock = threading.Lock()
_view = {"days": {}, "sessions": {}, "at": 0}   # 汇总好的：日期 → [新, 读]；会话 → {日期: [新, 读]}
_started = False


def _day(ts):
    """ISO 时间（UTC）→ 本地日期。"""
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t.astimezone().date().isoformat()


def _claude_sid(path):
    d = os.path.dirname(path)
    if os.path.basename(d) == "subagents":
        return os.path.basename(os.path.dirname(d))
    return os.path.basename(path)[:-6]


def _codex_sid(path):
    name = os.path.basename(path)[:-6]          # rollout-2026-10-25T00-46-19-<编号>
    return name[-36:] if len(name) > 36 else name


def _read(path, kind, rec):
    with open(path, "rb") as f:
        f.seek(rec["off"])
        data = f.read()
    end = data.rfind(b"\n") + 1                 # 最后一行可能还没写完，下次再读
    for ln in data[:end].splitlines():
        if kind == "claude":
            if b'"output_tokens"' not in ln or b'"assistant"' not in ln:
                continue
        elif b'"token_count"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        day = _day(d.get("timestamp"))
        if not day:
            continue
        if kind == "claude":
            m = d.get("message") or {}
            u = m.get("usage")
            if d.get("type") != "assistant" or not isinstance(u, dict) or not m.get("id"):
                continue
            new = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("output_tokens") or 0)
            rec["msgs"][m["id"]] = (day, new, u.get("cache_read_input_tokens") or 0)
        else:
            p = d.get("payload") or {}
            info = p.get("info") or {}
            u = info.get("last_token_usage")
            if p.get("type") != "token_count" or not isinstance(u, dict):
                continue
            cached = u.get("cached_input_tokens") or 0
            new = max(0, (u.get("input_tokens") or 0) - cached) + (u.get("output_tokens") or 0)
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            rec["msgs"][f"cx:{d.get('timestamp')}:{total}"] = (day, new, cached)
    rec["off"] += end


def _files():
    cut = time.time() - KEEP_DAYS * 86400
    home = os.path.expanduser("~")
    for kind, pat in (("claude", os.path.join(home, ".claude", "projects", "**", "*.jsonl")),
                      ("codex", os.path.join(home, ".codex", "sessions", "**", "*.jsonl"))):
        for p in glob.iglob(pat, recursive=True):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= cut:
                yield kind, p, st.st_size, getattr(st, "st_birthtime", st.st_ctime)


def refresh():
    """读新增的部分，重新汇总。"""
    seen = set()
    for kind, p, size, born in _files():
        seen.add(p)
        rec = _FILES.get(p)
        if rec is None or size < rec["size"]:     # 新文件，或被改短了（重写过）：从头读
            rec = _FILES[p] = {"size": 0, "off": 0, "born": born, "msgs": {},
                               "sid": _claude_sid(p) if kind == "claude" else _codex_sid(p)}
        if size == rec["size"]:
            continue
        try:
            _read(p, kind, rec)
        except OSError:
            continue
        rec["size"] = size
    for p in [p for p in _FILES if p not in seen]:
        del _FILES[p]
    owner = {}                                     # 消息编号 → (最早建立的文件的建立时间, 那个文件)
    for p, rec in _FILES.items():
        b = rec["born"]
        for k in rec["msgs"]:
            o = owner.get(k)
            if o is None or b < o[0]:
                owner[k] = (b, p)
    days, sessions = {}, {}
    for k, (_, p) in owner.items():
        rec = _FILES[p]
        day, new, read = rec["msgs"][k]
        t = days.setdefault(day, [0, 0])
        t[0] += new
        t[1] += read
        s = sessions.setdefault(rec["sid"], {}).setdefault(day, [0, 0])
        s[0] += new
        s[1] += read
    with _lock:
        _view.update(days=days, sessions=sessions, at=time.time())


def _loop():
    while True:
        try:
            refresh()
        except Exception:
            log.exception("统计 Token 出错")
        time.sleep(60)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="tokens").start()


def ready():
    return bool(_view["at"])


def days():
    """本机每天的用量：日期 → [新产生, 读缓存]。"""
    with _lock:
        return dict(_view["days"])


def of_sessions(sids):
    """几个会话合起来每天的用量。"""
    out = {}
    with _lock:
        for sid in sids:
            for day, (n, r) in (_view["sessions"].get(sid) or {}).items():
                t = out.setdefault(day, [0, 0])
                t[0] += n
                t[1] += r
    return out
