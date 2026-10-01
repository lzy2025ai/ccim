"""把 Claude Code 里正在进行的对话（桌面端、终端都行）转到飞书接着聊。

流程：`ccim handoff` 在那个对话里运行 → 读出对话记录（属于哪个项目、聊到哪、用的什么模型和思考深度）
→ 往项目的状态目录写一个转接请求 → ccim 进程每秒检查一次，把主人的私聊接到这个对话的分支上，
并主动发一条消息给主人（手机会收到推送）→ 写回结果。

运行 handoff 时，那个对话这一轮还没结束（最后一条是还没返回结果的命令调用），把这半截接过去恢复会出错，
所以只接到「用户说要转过去」之前的最后一条回复为止（resume_session_at）。
"""
import json, os, time, uuid

from . import registry

REQUEST = "handoff.json"
RESULT = "handoff.done.json"


def transcript_path(session_id, cwd=None):
    base = os.path.expanduser("~/.claude/projects")
    if cwd:
        p = os.path.join(base, os.path.realpath(cwd).replace("/", "-").replace(".", "-"), f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    for d in os.listdir(base) if os.path.isdir(base) else []:
        p = os.path.join(base, d, f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    return None


def _is_prompt(row):
    """用户真正说的话（不是工具结果、不是系统塞进来的内容）。"""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return any(b.get("type") in ("text", "image") for b in content or []) and \
        not any(b.get("type") == "tool_result" for b in content or [])


def _text(row):
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def read(session_id, cwd=None):
    """读对话记录，返回 {cwd, title, model, at, last_reply}；找不到返回 None。"""
    path = transcript_path(session_id, cwd)
    if not path:
        return None
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    title = cwd_ = model = None
    for r in rows:
        if r.get("type") == "custom-title":
            title = r.get("customTitle") or title
        cwd_ = r.get("cwd") or cwd_
        if r.get("type") == "assistant":
            model = (r.get("message") or {}).get("model") or model
    # 最后一句用户的话就是「转到飞书」那一轮的开头，接到它前面的最后一条回复为止
    prompts = [i for i, r in enumerate(rows) if _is_prompt(r)]
    end = prompts[-1] if prompts else len(rows)
    before = [r for r in rows[:end] if r.get("type") == "assistant" and not r.get("isSidechain")]
    at = before[-1]["uuid"] if before else None
    reply = ""
    for r in reversed(before):
        reply = _text(r).strip()
        if reply:
            break
    first = next((_text(rows[i]).strip() for i in prompts), "")
    return {"cwd": cwd_ or cwd, "title": title or " ".join(first.split())[:30], "model": model, "at": at,
            "last_reply": reply}


def request(path, req, timeout=30):
    """写转接请求，等 ccim 进程处理完，返回结果。"""
    d = registry.pdir(path)
    req = dict(req, id=uuid.uuid4().hex, at_time=registry.now())
    try:
        os.remove(os.path.join(d, RESULT))
    except FileNotFoundError:
        pass
    tmp = os.path.join(d, REQUEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, REQUEST))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            with open(os.path.join(d, RESULT), encoding="utf-8") as f:
                res = json.load(f)
            if res.get("id") == req["id"]:
                return res
        except (OSError, ValueError):
            pass
    return {"ok": False, "error": "ccim 没有响应"}


def take(path):
    """ccim 进程这边：取走一个待处理的转接请求。"""
    p = os.path.join(registry.pdir(path), REQUEST)
    try:
        with open(p, encoding="utf-8") as f:
            req = json.load(f)
    except (OSError, ValueError):
        return None
    os.remove(p)
    return req


def done(path, req, **result):
    p = os.path.join(registry.pdir(path), RESULT)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(dict(result, id=req.get("id")), f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def open_link(entry):
    """手机扫码直接打开和机器人的聊天。"""
    host = "applink.larksuite.com" if entry.get("domain") == "lark" else "applink.feishu.cn"
    return f"https://{host}/client/bot/open?appId={entry['app_id']}"
