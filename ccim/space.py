"""个人空间（#542）：用户自己的待办、收藏、笔记。头像点进去看，对话里 Agent 的回答「⋯」能收藏、转成待办。

存在这台电脑 ~/.ccim/space/<账号>.json（主人是 owner.json），访客各看各的。
动态里的收藏（定时任务的 stars）不搬过来：读的时候合进收藏，编号是 feed:<任务>:<那次运行>，取消收藏改回任务上。
几台电脑的时候，网页只找本机（S.me）那台存取，动态的收藏也只合那台上的。
"""
import json, os, re, secrets, threading, time

from . import registry

MAX_TEXT = 20000           # 收藏的回答、笔记正文最多存多少字
MAX_TODO = 500
_lock = threading.Lock()


class Problem(Exception):
    pass


def _file(acct):
    name = "owner" if not acct else "a-" + re.sub(r"[^A-Za-z0-9_-]", "_", str(acct))[:80]
    return os.path.join(registry.HOME, "space", name + ".json")


def _read(acct):
    try:
        with open(_file(acct), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    for k in ("todos", "favs", "notes"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def _write(acct, d):
    p = _file(acct)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, p)


def _id():
    return secrets.token_hex(5)


def _src(src):
    """来源：哪个对话的哪条回答。只留认识的字段。"""
    if not isinstance(src, dict):
        return None
    out = {k: str(src[k])[:200] for k in ("chat", "title", "agent", "machine") if isinstance(src.get(k), str) and src[k]}
    m = src.get("msg")
    if isinstance(m, (int, float, str)) and m != "":
        out["msg"] = m if not isinstance(m, str) else m[:80]
    return out if out.get("chat") else None


def _text(t, n):
    t = (t if isinstance(t, str) else "").strip()
    return t[:n]


# ---------- 读 ----------

def view(acct, starred=()):
    """整个个人空间。starred：动态里收藏的（Schedules.starred 给的），合进收藏。"""
    d = _read(acct)
    favs = [dict(f, kind="chat") for f in d["favs"]]
    for s in starred:
        favs.append({"id": f"feed:{s['sid']}:{s['rid']}", "kind": "feed", "text": s.get("text") or "", "at": s.get("at") or 0,
                     "src": {"name": s.get("name") or "", "agent": s.get("agent") or "", "chat": s.get("chat") or "", "sid": s["sid"]}})
    favs.sort(key=lambda f: -(f.get("at") or 0))
    notes = sorted(d["notes"], key=lambda n: -(n.get("updated") or n.get("at") or 0))
    return {"todos": d["todos"], "favs": favs, "notes": notes}


# ---------- 待办 ----------

def todo_add(acct, text, src=None):
    text = _text(text, 500)
    if not text:
        raise Problem("先写点什么。")
    with _lock:
        d = _read(acct)
        if len([t for t in d["todos"] if not t.get("done")]) >= MAX_TODO:
            raise Problem("待办太多了，先勾掉一些。")
        item = {"id": _id(), "text": text, "done": False, "at": time.time()}
        s = _src(src)
        if s:
            item["src"] = s
        d["todos"].insert(0, item)
        _write(acct, d)
    return {"item": item}


def todo_set(acct, tid, done=None, text=None):
    with _lock:
        d = _read(acct)
        t = next((x for x in d["todos"] if x.get("id") == tid), None)
        if not t:
            raise Problem("这条待办已经不在了。")
        if done is not None:
            t["done"] = bool(done)
            if t["done"]:
                t["done_at"] = time.time()
            else:
                t.pop("done_at", None)
        if text is not None:
            text = _text(text, 500)
            if not text:
                raise Problem("待办不能是空的。")
            t["text"] = text
        _write(acct, d)
    return {"item": t}


def todo_del(acct, tid):
    with _lock:
        d = _read(acct)
        d["todos"] = [x for x in d["todos"] if x.get("id") != tid]
        _write(acct, d)
    return {}


def todo_restore(acct, item):
    """撤销删除：原样放回去。"""
    if not isinstance(item, dict) or not item.get("id") or not _text(item.get("text"), 500):
        raise Problem("没法撤销了。")
    with _lock:
        d = _read(acct)
        if not any(x.get("id") == item["id"] for x in d["todos"]):
            keep = {"id": str(item["id"])[:20], "text": _text(item["text"], 500), "done": bool(item.get("done")),
                    "at": item.get("at") if isinstance(item.get("at"), (int, float)) else time.time()}
            if keep["done"]:
                keep["done_at"] = item.get("done_at") if isinstance(item.get("done_at"), (int, float)) else time.time()
            s = _src(item.get("src"))
            if s:
                keep["src"] = s
            d["todos"].append(keep)
            d["todos"].sort(key=lambda x: -(x.get("at") or 0))
            _write(acct, d)
    return {}


# ---------- 收藏（对话里的回答） ----------

def fav(acct, on, src, text=""):
    """收藏 / 取消收藏一条回答，按「对话 + 那条回答」认。"""
    s = _src(src)
    if not s or "msg" not in s:
        raise Problem("找不到这条回答。")
    with _lock:
        d = _read(acct)
        old = next((f for f in d["favs"] if (f.get("src") or {}).get("chat") == s["chat"] and (f.get("src") or {}).get("msg") == s["msg"]), None)
        if on and not old:
            text = _text(text, MAX_TEXT)
            if not text:
                raise Problem("这条回答没有内容。")
            old = {"id": _id(), "text": text, "at": time.time(), "src": s}
            d["favs"].insert(0, old)
        elif not on and old:
            d["favs"].remove(old)
        _write(acct, d)
    return {"id": old["id"] if on and old else None}


def fav_del(acct, fid):
    with _lock:
        d = _read(acct)
        d["favs"] = [f for f in d["favs"] if f.get("id") != fid]
        _write(acct, d)
    return {}


def feed_id(fid):
    """feed:<任务>:<那次运行> → (任务, 运行)；不是动态的收藏返回 None。"""
    if not isinstance(fid, str) or not fid.startswith("feed:"):
        return None
    parts = fid.split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 and parts[1] and parts[2] else None


# ---------- 笔记 ----------

def note_save(acct, nid, title, body):
    title, body = _text(title, 200), (body if isinstance(body, str) else "")[:MAX_TEXT]
    now = time.time()
    with _lock:
        d = _read(acct)
        n = next((x for x in d["notes"] if x.get("id") == nid), None) if nid else None
        if nid and not n:
            raise Problem("这篇笔记已经删掉了。")
        if not n:
            n = {"id": _id(), "at": now}
            d["notes"].insert(0, n)
        n.update(title=title, body=body, updated=now)
        _write(acct, d)
    return {"item": n}


def note_del(acct, nid):
    with _lock:
        d = _read(acct)
        d["notes"] = [x for x in d["notes"] if x.get("id") != nid]
        _write(acct, d)
    return {}
