"""节点和同题对比（#501）：~/.ccim/nodes.json。

节点 = 一件事的固定做法：名字、要给它什么（输入）、怎么做（含聊出来的注意事项）、交付什么。
在对话里聊清楚后「存为节点」（按对话内容起草，用户改过再存），下次填这次的输入就能再跑。
对比（run）= 同一段任务交给几个 Agent（或底座 + 模型），各开一个独立对话同时开始，互不影响；
记下这几个对话，对比页并排看。不用节点也能对比（直接写任务）。
以后节点串成流程、上画布，都在这上面加。
"""
import json, os, tempfile, time, uuid

from . import registry

KEEP_RUNS = 100          # 对比记录留最近这么多条（对话本身不删）
MAX_RUNNERS = 3
FIELDS = ("name", "input", "steps", "output")
LIMITS = {"name": 40, "input": 4000, "steps": 20000, "output": 4000}


class Problem(Exception):
    pass


def _file():
    return os.path.join(registry.HOME, "nodes.json")


def _read():
    try:
        with open(_file(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("nodes", {})
    d.setdefault("runs", {})
    return d


def _write(d):
    os.makedirs(registry.HOME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=registry.HOME, prefix=".nodes.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _file())


def _mine(x, account):
    return (x.get("account") or None) == (account or None)


def listing(account=None):
    d = _read()
    nodes = [dict(n, id=k) for k, n in d["nodes"].items() if _mine(n, account)]
    nodes.sort(key=lambda n: n.get("updated") or "", reverse=True)
    runs = [dict(r, id=k) for k, r in d["runs"].items() if _mine(r, account)]
    runs.sort(key=lambda r: r.get("created") or "", reverse=True)
    for n in nodes:
        n["runs"] = sum(1 for r in runs if r.get("node") == n["id"])
    return {"nodes": nodes, "runs": runs[:30]}


def get(nid, account=None):
    n = _read()["nodes"].get(nid or "")
    if not n or not _mine(n, account):
        raise Problem("找不到这个节点。")
    return dict(n, id=nid)


def save(nid, fields, account=None, from_chat=None):
    d = _read()
    clean = {k: str(fields.get(k) or "").strip()[:LIMITS[k]] for k in FIELDS}
    if not clean["name"]:
        raise Problem("给节点起个名字。")
    if not clean["steps"]:
        raise Problem("写一下怎么做。")
    if nid:
        n = d["nodes"].get(nid)
        if not n or not _mine(n, account):
            raise Problem("找不到这个节点。")
    else:
        nid = "n-" + uuid.uuid4().hex[:10]
        n = d["nodes"][nid] = {"created": registry.now()}
        if account:
            n["account"] = account
        if from_chat:
            n["from_chat"] = from_chat
    n.update(clean, updated=registry.now())
    _write(d)
    return dict(n, id=nid)


def delete(nid, account=None):
    d = _read()
    n = d["nodes"].get(nid or "")
    if not n or not _mine(n, account):
        raise Problem("找不到这个节点。")
    del d["nodes"][nid]
    _write(d)
    return {}


def compose(node, text):
    """交给 Agent 的第一句话：节点的做法 + 这次的输入。"""
    parts = [f"按下面这套做法完成「{node['name']}」。"]
    if node.get("steps"):
        parts.append("## 怎么做\n" + node["steps"])
    if node.get("output"):
        parts.append("## 交付什么\n" + node["output"])
    parts.append("## 这次的输入\n" + (text.strip() or "（没有额外输入，按上面的做法直接做）"))
    return "\n\n".join(parts)


def run_title(node, text):
    if node:
        return node["name"][:30]
    t = " ".join((text or "").split())
    return t[:24] + ("…" if len(t) > 24 else "")


def add_run(node_id, text, chats, title, account=None):
    d = _read()
    rid = "r-" + uuid.uuid4().hex[:10]
    rec = {"node": node_id, "input": text[:4000], "chats": chats, "title": title, "created": registry.now(), "ts": time.time()}
    if account:
        rec["account"] = account
    d["runs"][rid] = rec
    if len(d["runs"]) > KEEP_RUNS:
        for k in sorted(d["runs"], key=lambda k: d["runs"][k].get("ts") or 0)[:len(d["runs"]) - KEEP_RUNS]:
            del d["runs"][k]
    _write(d)
    return rid


def get_run(rid, account=None):
    r = _read()["runs"].get(rid or "")
    if not r or not _mine(r, account):
        raise Problem("找不到这次对比。")
    return dict(r, id=rid)


def delete_run(rid, account=None):
    d = _read()
    r = d["runs"].get(rid or "")
    if not r or not _mine(r, account):
        raise Problem("找不到这次对比。")
    del d["runs"][rid]
    _write(d)
    return {}


def add_chat(rid, chat, account=None):
    """对比里再加一个（比如漏选了一个模型）。"""
    d = _read()
    r = d["runs"].get(rid or "")
    if not r or not _mine(r, account):
        raise Problem("找不到这次对比。")
    r["chats"].append(chat)
    _write(d)


DRAFT_PROMPT = """下面是用户和 AI 助手的一段对话。用户想把对话里聊清楚的这件事的做法固定成一个「节点」：以后换个输入，交给任意一个 AI 助手照着做，不用再从头聊。

请从对话里提炼出：
1. 名字：这件事叫什么，不超过 16 个字。
2. 要给它什么：每次运行时用户要提供的输入，一两句话。
3. 怎么做：具体步骤和要求。对话里用户纠正过的、指出的问题、定下来的规矩，都要写进去，写成正面的要求（比如「不要把早已存在的产品分级当成新变化」）。不要写这次对话里的具体内容（比如这次是哪个产品），要写成换个输入也能用的做法。
4. 交付什么：最后交出来的东西是什么样的，格式和要包含的部分。

对话：
{talk}

按这个格式输出，不要别的内容：
【名字】……
【要给它什么】……
【怎么做】
……
【交付什么】
……"""

_KEYS = {"名字": "name", "要给它什么": "input", "怎么做": "steps", "交付什么": "output"}


def parse_draft(text):
    out, key, buf = {}, None, []

    def flush():
        if key:
            out[key] = "\n".join(buf).strip()
    for line in (text or "").splitlines():
        s = line.strip()
        hit = next((k for k in _KEYS if s.startswith(f"【{k}】")), None)
        if hit:
            flush()
            key, buf = _KEYS[hit], [s[len(hit) + 2:].strip()]
        elif key:
            buf.append(line.rstrip())
    flush()
    if out.get("name"):
        out["name"] = " ".join(out["name"].split()).strip("「」\"'“”")[:20]
    return {k: out.get(k, "") for k in FIELDS}


def talk_of(rows):
    lines = [("用户：" if r["type"] == "user" else "助手：") + r["text"].strip()[:3000]
             for r in rows if r.get("type") in ("user", "answer") and r.get("text")]
    talk = "\n\n".join(lines)
    if len(talk) > 30000:
        talk = talk[:8000] + "\n\n……\n\n" + talk[-22000:]
    return talk


async def draft(rows):
    """按对话起草节点。"""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
    from .session import CLI
    talk = talk_of(rows)
    if not talk:
        return {k: "" for k in FIELDS}
    opts = ClaudeAgentOptions(model="sonnet", max_turns=1, allowed_tools=[], setting_sources=[],
                              cwd=os.path.expanduser("~"), cli_path=CLI, permission_mode="default",
                              extra_args={"no-session-persistence": None})   # 不留对话记录
    text = ""
    async for msg in query(prompt=DRAFT_PROMPT.format(talk=talk), options=opts):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return parse_draft(text)
