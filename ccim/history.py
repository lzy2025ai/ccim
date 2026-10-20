"""把飞书、微信、终端里的对话读成和网页对话一样的记录（user / answer），网页里打开就能看。

Claude Code：~/.claude/projects/<目录>/<session_id>.jsonl（用户说的话、助手的文字和工具调用）。
Codex：~/.codex/sessions/年/月/日/rollout-…-<thread_id>.jsonl，读 event_msg 里 item_completed 的
UserMessage / AgentMessage / CommandExecution；用户消息开头 ccim 加的说明要去掉。
"""
import calendar, glob, json, os, re, threading, time

from . import handoff
from .progress import describe_tool

NOTE_END = "有问题直接在回复里问。\n\n"     # codex.NOTE 的最后一句，用户的话从它后面开始


def _ts(iso):
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


SYS_BLOCK = re.compile(r"<(system-reminder|task-notification|local-command-stdout|local-command-caveat)>[\s\S]*?</\1>")


def claude(session_id, cwd=None, limit=200):
    path = handoff.transcript_path(session_id, cwd)
    if not path:
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("isSidechain") or r.get("type") not in ("user", "assistant"):
                continue
            at = _ts(r.get("timestamp", ""))
            if handoff._is_prompt(r):
                # 系统塞进来的提醒、后台任务通知不是用户说的话，去掉；剩下的整段是标签（斜杠命令等）也跳过
                text = SYS_BLOCK.sub("", handoff._text(r)).strip()
                if not text or (text.startswith("<") and text.endswith(">")):
                    continue
                out.append({"type": "user", "text": text, "files": [], "at": at})
                continue
            if r.get("type") != "assistant":
                continue
            if not out or out[-1]["type"] != "answer":
                out.append({"type": "answer", "text": "", "steps": [], "state": "done", "at": at})
            ans = out[-1]
            for b in (r.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    d = describe_tool(b.get("name", ""), b.get("input"))
                    if d:
                        ans["steps"].append(d)
                elif b.get("type") == "text" and b.get("text", "").strip():
                    ans["text"] = b["text"].strip()            # 一轮里最后一段文字就是回答
    return out[-limit:]


_UUIDS = {}                                  # 对话记录 → [读到哪, 消息编号（按顺序）, 编号集合]，只读新增的部分
_LOCK = threading.Lock()


def _uuids(path):
    with _LOCK:
        return _read_uuids(path)


def _read_uuids(path):
    c = _UUIDS.get(path)
    size = os.path.getsize(path)
    if not c or size < c[0]:
        c = _UUIDS[path] = [0, [], set()]
    if size > c[0]:
        with open(path, "rb") as f:
            f.seek(c[0])
            data = f.read()
        end = data.rfind(b"\n") + 1               # 最后一行可能还没写完，下次再读
        for line in data[:end].splitlines():
            if b'"uuid"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("type") in ("user", "assistant") and not r.get("isSidechain") \
                    and r.get("uuid") and r["uuid"] not in c[2]:
                c[1].append(r["uuid"])
                c[2].add(r["uuid"])
        c[0] += end
    return c


def fork_parents(session_ids, cwd=None):
    """终端里分叉出来的 Claude Code 对话是从哪个分出来的：分叉会把之前的消息原样（编号不变）复制到新对话，
    所以开头的消息在别的对话里也有的就是分叉。从共用得最多的那个分出来；一样多时算最早的那个（原来的）。"""
    info = {}
    for sid in session_ids:
        path = handoff.transcript_path(sid, cwd)
        if not path:
            continue
        try:
            st = os.stat(path)
            info[sid] = (getattr(st, "st_birthtime", st.st_mtime), _uuids(path))
        except OSError:
            continue
    out = {}
    for b, (born, (_, seq, _s)) in info.items():
        if not seq:
            continue
        best = None
        for a, (a_born, (_, _q, aset)) in info.items():
            if a == b or a_born >= born or seq[0] not in aset:
                continue
            n = 0
            for u in seq:
                if u not in aset:
                    break
                n += 1
            if n == len(seq) and n == len(_q):       # 内容完全一样，分不出来
                continue
            if not best or n > best[0] or (n == best[0] and a_born < best[1]):
                best = (n, a_born, a)
        if best:
            out[b] = best[2]
    return out


def codex_file(thread_id):
    hits = glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*-{thread_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def codex(thread_id, limit=200):
    path = codex_file(thread_id)
    if not path:
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("payload"), dict):
                continue
            p = r["payload"]
            if r.get("type") != "event_msg" or p.get("type") != "item_completed":
                continue
            item, at = p.get("item") or {}, _ts(r.get("timestamp", ""))
            text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)).strip()
            kind = item.get("type")
            if kind == "UserMessage":
                if NOTE_END in text:
                    text = text.split(NOTE_END, 1)[1]
                out.append({"type": "user", "text": text.strip(), "files": [], "at": at})
            elif kind in ("AgentMessage", "CommandExecution"):
                if not out or out[-1]["type"] != "answer":
                    out.append({"type": "answer", "text": "", "steps": [], "state": "done", "at": at})
                if kind == "AgentMessage" and text:     # 「[发送文件] 路径」那行只留文件名
                    out[-1]["text"] = re.sub(r"(?m)^\s*\[发送文件\]\s*(.+)$", lambda m: f"（发了文件：{os.path.basename(m.group(1).strip())}）", text)
                elif kind == "CommandExecution":
                    cmd = item.get("command") or []
                    cmd = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
                    out[-1]["steps"].append("运行：" + re.sub(r"\s+", " ", cmd)[:80])
    return out[-limit:]


_titles = {}


def title(base, session_id, cwd=None):
    """对话的第一句话，当标题（标题功能上线前的老对话没记标题）。按记录文件的修改时间缓存。"""
    path = codex_file(session_id) if base == "codex" else handoff.transcript_path(session_id, cwd)
    try:
        key = (path, os.path.getmtime(path)) if path else None
    except OSError:
        return None
    if not key:
        return None
    if key not in _titles:
        first = next((r["text"] for r in (codex(session_id, 400) if base == "codex" else claude(session_id, cwd, 400))
                      if r["type"] == "user" and r["text"]), "")
        _titles[key] = " ".join(first.split())[:30] or None
    return _titles[key]


_READ = {}                                   # (底座, 对话) → (文件大小, 修改时间, 记录)：没变就不重读（#32）


def read(base, session_id, cwd=None):
    path = codex_file(session_id) if base == "codex" else handoff.transcript_path(session_id, cwd)
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    key, sig = (base, session_id), (st.st_size, st.st_mtime) if st else None
    hit = _READ.get(key)
    if sig and hit and hit[0] == sig:
        return [dict(r) for r in hit[1]]
    rows = codex(session_id) if base == "codex" else claude(session_id, cwd)
    if sig:
        if len(_READ) > 50:
            _READ.clear()
        _READ[key] = (sig, rows)
    return [dict(r) for r in rows]
