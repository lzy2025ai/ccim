"""把 Claude Code 里正在进行的对话（桌面端、终端都行）转到飞书接着聊，以及回到电脑前再接回来。

流程：`ccim handoff` 在那个对话里运行 → 读出对话记录（属于哪个项目、聊到哪、用的什么模型和思考深度）
→ 往项目的状态目录写一个转接请求 → ccim 进程每秒检查一次，把主人的私聊接到这个对话的分支上，
并主动发一条消息给主人（手机会收到推送）→ 写回结果。

运行 handoff 时，那个对话这一轮还没结束（最后一条是还没返回结果的命令调用），把这半截接过去恢复会出错，
所以只接到「用户说要转过去」之前的最后一条回复为止（resume_session_at）。
"""
import calendar, html, json, os, subprocess, time, uuid

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


def show_qr_page(path, url):
    """第一次转接时配对：在浏览器里打开一个带二维码的页面（在 Claude 桌面端里看不到命令行输出）。"""
    import qrcode
    import qrcode.image.svg
    svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2).to_string(encoding="unicode")
    name = html.escape(os.path.basename(path))
    page = f"""<!doctype html><meta charset="utf-8"><title>把「{name}」接入飞书</title>
<style>body{{font:16px -apple-system,"PingFang SC",sans-serif;display:flex;flex-direction:column;align-items:center;
margin-top:8vh;color:#1f2329}} h1{{font-size:22px;margin:0 0 8px}} p{{color:#646a73;margin:4px}}
.qr{{width:300px;margin:24px}} .qr svg{{width:100%;height:auto}} a{{color:#3370ff;font-size:13px}}</style>
<h1>用飞书扫码，把「{name}」接入飞书</h1>
<p>确认后会在你的飞书账号下建好一个机器人，然后自动接上刚才的对话。</p>
<div class="qr">{svg}</div>
<p>扫完回到 Claude 就行，这个页面可以关掉。</p>
<p><a href="{html.escape(url)}">扫不了的话，在手机飞书里打开这个链接</a></p>"""
    f = os.path.join(registry.pdir(path), "pair.html")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(page)
    subprocess.run(["open", f], capture_output=True)
    print(f"二维码已在浏览器里打开：{f}\n扫不了的话，在手机飞书里打开：{url}", flush=True)


# ---------- 接回来（ccim handback） ----------

def find_branch(path, session_id):
    """找到从这个对话转出去的飞书聊天：返回 (chat_id, 记录)；没转出去过返回 (None, None)。"""
    chats = registry.read_state(path).get("chats") or {}
    hits = [(k, c) for k, c in chats.items() if c.get("handoff_from") == session_id]
    if not hits:
        return None, None
    return max(hits, key=lambda kv: kv[1].get("handoff_time", ""))


def digest(path, session_id, since):
    """飞书分支里 since（UTC ISO）之后的内容，整理成按时间排的文字。返回 (文字, 轮数)。"""
    from .progress import describe_tool
    tp = transcript_path(session_id, path)
    if not tp:
        return "", 0
    rows = []
    with open(tp, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("timestamp", "") > since and not r.get("isSidechain") and r.get("type") in ("user", "assistant"):
                rows.append(r)
    turns, files = [], {}
    for r in rows:
        when = _local(r.get("timestamp", ""))
        if _is_prompt(r):
            turns.append({"when": when, "user": _text(r).strip(), "steps": [], "reply": ""})
            continue
        if r.get("type") != "assistant" or not turns:
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                d = describe_tool(b.get("name", ""), b.get("input"))
                if d:
                    turns[-1]["steps"].append(d)
                fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                if fp and b.get("name") in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                    files[fp] = "新建或重写" if b.get("name") == "Write" else "修改"
            elif b.get("type") == "text" and b.get("text", "").strip():
                turns[-1]["reply"] = b["text"].strip()
    out = []
    for t in turns:
        out.append(f"[{t['when']}] 用户：{_cap(t['user'], 1500)}")
        if t["steps"]:
            steps = t["steps"] if len(t["steps"]) <= 12 else t["steps"][:5] + [f"……共 {len(t['steps'])} 步……"] + t["steps"][-5:]
            out.append("  Claude 的操作：" + "；".join(steps))
        out.append(f"  Claude：{_cap(t['reply'], 3000) or '（没有文字回复）'}")
        out.append("")
    if files:
        out.append("改动过的文件（通过命令改的不在此列，可以再看 git status）：")
        out += [f"- {p}（{how}）" for p, how in files.items()]
    return "\n".join(out).strip(), len(turns)


def _cap(s, n):
    return s if len(s) <= n else s[:n] + f"……（后面还有 {len(s) - n} 字）"


def _local(ts):
    try:
        t = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return time.strftime("%m-%d %H:%M", time.localtime(calendar.timegm(t)))
    except ValueError:
        return ts[:16]
