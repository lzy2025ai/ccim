"""节点和同题对比（#501）：~/.ccim/nodes.json。

节点 = 一件事的固定做法：名字、要给它什么（输入）、怎么做（含聊出来的注意事项）、交付什么，和由谁来做（runner）。
在对话里聊清楚后「存为节点」：由这个对话的 Agent 自己用 save_node 工具写（它有完整的上下文），
节点就记下它（Agent 的对话记 Agent，临时对话记当时的底座和模型），运行时默认交给它。
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


def clean_runner(r):
    """{"agent": 编号} 或 {"temp": {"base", "model", "effort"}}；不认识的当 Claude Code 默认设置。"""
    r = r if isinstance(r, dict) else {}
    if isinstance(r.get("agent"), str) and r["agent"]:
        return {"agent": r["agent"]}
    t = r.get("temp") if isinstance(r.get("temp"), dict) else {}
    return {"temp": {"base": t.get("base") if t.get("base") in registry.BASES else "claude",
                     "model": str(t.get("model") or "")[:80] or None, "effort": str(t.get("effort") or "")[:20] or None}}


def save(nid, fields, account=None, from_chat=None, runner=None):
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
    if runner is not None or not n.get("runner"):
        n["runner"] = clean_runner(runner)
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
