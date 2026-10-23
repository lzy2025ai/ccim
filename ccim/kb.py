"""知识库（#27）：把有价值的回答存下来，多台电脑同步，挂到对话里让助手查。

存在 ~/.ccim/kb/<库>/ 下：_lib.json 是库的名字、说明、颜色；每个条目一个 <条目>.md，第一行是一段注释（编号、标题、来源、
修改时间），后面是「# 标题」和正文，助手直接用读文件、搜索工具查。删掉的库和条目记在 _tombstones.json（键 → 删除时间），
同步时据此删别的电脑上的那份。

同步：每台电脑都存一份完整的。各自定期问在线的其他电脑要清单（每项的修改时间、删除记录），比自己新的拉过来，
所以哪台改的都会传开，离线的连上后补齐；服务器只转发，不存内容。两边改了同一条，后改的算数。
"""
import json, logging, os, re, threading, time, uuid

from . import registry

log = logging.getLogger("ccim.kb")
LOCK = threading.RLock()
COLORS = ["qing", "zhe", "lu", "zhu", "dai", "huang"]
HEAD = re.compile(r"^<!-- ccim (\{.*\}) -->\n")


def root():
    return os.path.join(registry.HOME, "kb")


def _lib_dir(lib):
    if not re.fullmatch(r"k[0-9a-f]{8,}", lib or ""):
        raise ValueError("找不到这个知识库。")
    return os.path.join(root(), lib)


def _entry_path(lib, eid):
    if not re.fullmatch(r"e[0-9a-f]{8,}", eid or ""):
        raise ValueError("找不到这条。")
    return os.path.join(_lib_dir(lib), eid + ".md")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _tombs():
    try:
        with open(os.path.join(root(), "_tombstones.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _bury(key, at=None):
    t = _tombs()
    t[key] = max(t.get(key, 0), at or time.time())
    _write(os.path.join(root(), "_tombstones.json"), json.dumps(t))


# ----- 读 -----

def _read_lib(lib):
    try:
        with open(os.path.join(_lib_dir(lib), "_lib.json"), encoding="utf-8") as f:
            return dict(json.load(f), id=lib)
    except (OSError, ValueError):
        return None


def _read_entry(lib, eid):
    try:
        with open(_entry_path(lib, eid), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = HEAD.match(text)
    meta = json.loads(m.group(1)) if m else {}
    body = text[m.end():] if m else text
    title = meta.get("title") or ""
    if body.startswith("# "):                 # 去掉给助手看的标题行
        body = body.split("\n", 1)[1] if "\n" in body else ""
    return {**meta, "id": eid, "lib": lib, "title": title, "content": body.lstrip("\n")}


def libs():
    out = []
    if not os.path.isdir(root()):
        return out
    for lib in sorted(os.listdir(root())):
        info = _read_lib(lib) if lib.startswith("k") else None
        if not info:
            continue
        es = [_read_entry(lib, f[:-3]) for f in os.listdir(_lib_dir(lib)) if f.endswith(".md")]
        es = [e for e in es if e]
        info["count"] = len(es)
        info["updated"] = max([info.get("updated", 0)] + [e.get("updated", 0) for e in es])
        out.append(info)
    out.sort(key=lambda x: -x["updated"])
    return out


def _preview(text):
    return " ".join(re.sub(r"[*_`#>]|\[|\]\([^)]*\)", "", text).split())[:120]


def lib_detail(lib):
    info = _read_lib(lib)
    if not info:
        raise ValueError("找不到这个知识库。")
    es = [_read_entry(lib, f[:-3]) for f in os.listdir(_lib_dir(lib)) if f.endswith(".md")]
    es = sorted((e for e in es if e), key=lambda e: -e.get("created", e.get("updated", 0)))
    info["entries"] = [{k: e.get(k) for k in ("id", "title", "source", "updated", "created")} |
                       {"preview": _preview(e["content"])} for e in es]
    info["dir"] = _lib_dir(lib)
    return info


def entry(lib, eid):
    e = _read_entry(lib, eid)
    if not e:
        raise ValueError("找不到这条。")
    return e


def search(q):
    q = (q or "").strip().lower()
    out = []
    if not q:
        return out
    for info in libs():
        for f in os.listdir(_lib_dir(info["id"])):
            if not f.endswith(".md"):
                continue
            e = _read_entry(info["id"], f[:-3])
            if e and (q in e["title"].lower() or q in e["content"].lower()):
                out.append({"lib": info["id"], "lib_name": info["name"], "id": e["id"], "title": e["title"],
                            "preview": _preview(e["content"]), "updated": e.get("updated")})
    return out[:100]


def dirs(ids):
    """挂到对话里的库：[(名字, 目录)]，找不到的跳过。"""
    out = []
    for lib in ids or []:
        try:
            info = _read_lib(lib)
        except ValueError:
            continue
        if info:
            out.append((info["name"], _lib_dir(lib)))
    return out


# ----- 写 -----

def save_lib(lib, name, desc="", color=None, account=None):
    name = " ".join((name or "").split())[:40]
    if not name:
        raise ValueError("给知识库起个名字。")
    with LOCK:
        old = _read_lib(lib) if lib else None
        if lib and not old:
            raise ValueError("找不到这个知识库。")
        lib = lib or "k" + uuid.uuid4().hex[:10]
        n = len(libs())
        info = {"name": name, "desc": (desc or "").strip()[:200], "color": color if color in COLORS else
                (old or {}).get("color") or COLORS[n % len(COLORS)], "created": (old or {}).get("created") or time.time(),
                "updated": time.time()}
        if (old or {}).get("account") or account:     # 共享电脑上访客的库（#154），建的时候定
            info["account"] = (old or {}).get("account") or account
        _write(os.path.join(_lib_dir(lib), "_lib.json"), json.dumps(info, ensure_ascii=False))
    return dict(info, id=lib)


def delete_lib(lib):
    with LOCK:
        d = _lib_dir(lib)
        if not os.path.isdir(d):
            return {}
        for f in os.listdir(d):
            if f.endswith(".md"):
                _bury(f"{lib}/{f[:-3]}")
            os.remove(os.path.join(d, f))
        os.rmdir(d)
        _bury(lib)
    return {}


def _render(meta, content):
    return f"<!-- ccim {json.dumps(meta, ensure_ascii=False)} -->\n# {meta['title']}\n\n{content.strip()}\n"


def save_entry(lib, eid, title, content, source=None):
    title = " ".join((title or "").split())[:80]
    content = (content or "").strip()
    if not content:
        raise ValueError("内容是空的。")
    with LOCK:
        if not _read_lib(lib):
            raise ValueError("找不到这个知识库。")
        old = _read_entry(lib, eid) if eid else None
        if eid and not old:
            raise ValueError("找不到这条。")
        eid = eid or "e" + uuid.uuid4().hex[:10]
        meta = {"id": eid, "title": title or content.split("\n", 1)[0][:40], "updated": time.time(),
                "created": (old or {}).get("created") or time.time(), "source": (old or {}).get("source") or source}
        _write(_entry_path(lib, eid), _render(meta, content))
    return entry(lib, eid)


def delete_entry(lib, eid):
    with LOCK:
        try:
            os.remove(_entry_path(lib, eid))
        except OSError:
            pass
        _bury(f"{lib}/{eid}")
    return {}


def move_entry(lib, eid, to):
    e = entry(lib, eid)
    with LOCK:
        if not _read_lib(to):
            raise ValueError("找不到这个知识库。")
        meta = {k: e[k] for k in ("id", "title", "created", "source") if k in e} | {"updated": time.time()}
        _write(_entry_path(to, eid), _render(meta, e["content"]))
        if to != lib:
            os.remove(_entry_path(lib, eid))
            _bury(f"{lib}/{eid}")
    return {"lib": to}


# ----- 同步 -----

def manifest():
    """{键: 修改时间}（库是「k…」，条目是「k…/e…」）和删除记录。"""
    items = {}
    if os.path.isdir(root()):
        for lib in os.listdir(root()):
            info = _read_lib(lib) if lib.startswith("k") else None
            if not info:
                continue
            items[lib] = info.get("updated", 0)
            for f in os.listdir(_lib_dir(lib)):
                if f.endswith(".md"):
                    e = _read_entry(lib, f[:-3])
                    if e:
                        items[f"{lib}/{e['id']}"] = e.get("updated", 0)
    return {"items": items, "tombs": _tombs()}


def export(keys):
    """把这些键的原文交给别的电脑。"""
    out = {}
    for k in keys or []:
        try:
            if "/" in k:
                lib, eid = k.split("/", 1)
                with open(_entry_path(lib, eid), encoding="utf-8") as f:
                    out[k] = f.read()
            else:
                with open(os.path.join(_lib_dir(k), "_lib.json"), encoding="utf-8") as f:
                    out[k] = f.read()
        except (OSError, ValueError):
            continue
    return out


def wanted(remote):
    """对方的清单里哪些比我新（要拉），以及对方删掉了、我这还留着的（要删）。"""
    mine = manifest()
    tombs, items = mine["tombs"], mine["items"]
    pull = [k for k, t in (remote.get("items") or {}).items()
            if t > items.get(k, -1) and t > tombs.get(k, -1) and t > tombs.get(k.split("/")[0], -1)]
    drop = [k for k, t in (remote.get("tombs") or {}).items() if k in items and t >= items[k]]
    return pull, drop, remote.get("tombs") or {}


def apply(contents, drop, tombs):
    changed = False
    with LOCK:
        for k in sorted(contents, key=lambda k: "/" in k):     # 先建库，再放条目
            try:
                if "/" in k:
                    lib, eid = k.split("/", 1)
                    if not os.path.isdir(_lib_dir(lib)):
                        continue
                    _write(_entry_path(lib, eid), contents[k])
                else:
                    json.loads(contents[k])
                    _write(os.path.join(_lib_dir(k), "_lib.json"), contents[k])
                changed = True
            except (ValueError, OSError):
                log.warning("同步过来的 %s 写不进去", k, exc_info=True)
        for k in sorted(drop, key=lambda k: "/" not in k):     # 先删条目，再删库
            try:
                if "/" in k:
                    lib, eid = k.split("/", 1)
                    os.remove(_entry_path(lib, eid))
                else:
                    d = _lib_dir(k)
                    for f in os.listdir(d):
                        os.remove(os.path.join(d, f))
                    os.rmdir(d)
                changed = True
            except (OSError, ValueError):
                pass
        mine = _tombs()
        merged = {k: max(v, mine.get(k, 0)) for k, v in tombs.items()}
        if any(mine.get(k) != v for k, v in merged.items()):
            _write(os.path.join(root(), "_tombstones.json"), json.dumps({**mine, **merged}))
    return changed
