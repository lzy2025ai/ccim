"""个人空间（#542）：用户自己的待办、笔记。头像点进去看。

待办的来源：自己记的、对话里某条回答「转成待办」、对话列表里「加到待办」（#554，来源只有对话、没有那条回答）、
动态里某条结果「加到待办」（来源是 feed + run）。对话列表上的待办标识就看这个对话有没有没做完的待办，不另外存。

存在这台电脑 ~/.ccim/space/<账号>.json（主人是 owner.json），访客各看各的。几台电脑的时候，网页只找本机（S.me）那台存取。
#554 起不再有收藏：文件里老的 favs 原样留着，不读不删。
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
    """来源：哪个对话（的哪条回答），或动态里哪个任务的哪次运行。只留认识的字段。"""
    if not isinstance(src, dict):
        return None
    out = {k: str(src[k])[:200] for k in ("chat", "title", "agent", "machine", "feed", "run", "name") if isinstance(src.get(k), str) and src[k]}
    m = src.get("msg")
    if out.get("chat") and isinstance(m, (int, float, str)) and m != "":
        out["msg"] = m if not isinstance(m, str) else m[:80]
    if out.get("chat"):
        out.pop("feed", None); out.pop("run", None)
        return out
    return out if out.get("feed") and out.get("run") else None


def _text(t, n):
    t = (t if isinstance(t, str) else "").strip()
    return t[:n]


# ---------- 读 ----------

def view(acct, notes=True):
    """整个个人空间。"""
    d = _read(acct)
    if not notes:
        return {"todos": d["todos"]}
    return {"todos": d["todos"], "notes": sorted(d["notes"], key=lambda n: -(n.get("updated") or n.get("at") or 0))}


# ---------- 待办 ----------

def _add(d, text, src, quote=None):
    if len([t for t in d["todos"] if not t.get("done")]) >= MAX_TODO:
        raise Problem("待办太多了，先勾掉一些。")
    item = {"id": _id(), "text": text, "done": False, "at": time.time()}
    s = _src(src)
    if s:
        item["src"] = s
        q = _text(quote, QUOTE_MAX)
        if q:
            item["quote"] = q             # 加的时候那条回答 / 动态的原话（#588），待办里看得到当时说了什么
    d["todos"].insert(0, item)
    return item


QUOTE_MAX = 400


def todo_add(acct, text, src=None, quote=None):
    text = _text(text, 500)
    if not text:
        raise Problem("先写点什么。")
    with _lock:
        d = _read(acct)
        item = _add(d, text, src, quote)
        _write(acct, d)
    return {"item": item}


def todo_chat(acct, chat, on, src=None):
    """对话列表里的「加到待办」/「完成待办」（#554）。
    加：这个对话已经有没做完的待办就不再加；没有就加一条，内容是对话标题。
    完成：这个对话名下没做完的待办（包括从某条回答转来的）全部勾掉，返回勾掉的编号，好撤销。"""
    if not isinstance(chat, str) or not chat:
        raise Problem("找不到这个对话。")
    with _lock:
        d = _read(acct)
        mine = [t for t in d["todos"] if not t.get("done") and (t.get("src") or {}).get("chat") == chat]
        if on:
            if mine:
                return {"item": mine[0]}
            s = dict(src) if isinstance(src, dict) else {}
            s["chat"] = chat
            s.pop("msg", None)
            item = _add(d, _text(s.get("title"), 500) or "对话", s)
            _write(acct, d)
            return {"item": item}
        now = time.time()
        for t in mine:
            t["done"], t["done_at"] = True, now
        if mine:
            _write(acct, d)
        return {"ids": [t["id"] for t in mine]}


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
                q = _text(item.get("quote"), QUOTE_MAX)
                if q:
                    keep["quote"] = q
            d["todos"].append(keep)
            d["todos"].sort(key=lambda x: -(x.get("at") or 0))
            _write(acct, d)
    return {}


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
