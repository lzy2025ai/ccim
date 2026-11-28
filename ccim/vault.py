"""个人空间的 Markdown 笔记（#555）。配置跟网页账号走，文件在选中的电脑上。"""
import base64
import binascii
from collections import defaultdict
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import stat
import tempfile
import threading
from urllib.parse import unquote

from . import devices, registry, workspace
from .peers import Offline

API = "/api/space/vault"
PREFS = API + "s"
SELECT = PREFS + "/select"
NOTE_MAX = 4 * 1024 * 1024
IMAGE_MAX = 10 * 1024 * 1024
IMAGES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
          "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp", "ico": "image/x-icon",
          "tif": "image/tiff", "tiff": "image/tiff", "svg": "image/svg+xml"}
DEVICE_LOCK = threading.RLock()
PREF_LOCKS = defaultdict(threading.Lock)
DEVICE_WRITES = {"register", "import", "save", "create", "rename", "trash", "daily_open", "link", "attach", "append"}


class Problem(ValueError):
    pass


def _state(acct, kind):
    key = hashlib.sha256((acct or "owner").encode()).hexdigest()
    return Path(registry.HOME) / "vaults" / f"{kind}-{key}.json"


def _json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise Problem("笔记设置的格式不对。")
    return value


def _write(path, data, *, create=False):
    """原子保存。创建时保留已有文件，包括 Obsidian 同时新建的日记。"""
    fd, tmp = tempfile.mkstemp(prefix=".ccim-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            if not create and path.exists():
                os.fchmod(f.fileno(), stat.S_IMODE(path.stat().st_mode))
            f.write(data)
        if create:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, json.dumps(state, ensure_ascii=False).encode())


def _public(state):
    migration = state.get("migration") or {}
    pending = [{"machine": mid, "reason": reason} for mid, reason in (migration.get("remaining") or {}).items()]
    return {"recent": state.get("recent", []), "pending": pending, "aliases": state.get("aliases", {}),
            "imported_into": (migration.get("target") or {}).get("id", "")}


def preferences(acct):
    return _public(_json(_state(acct, "recent")))


def _peer_call(call, machine, method, route, body=b""):
    mid, name = machine["id"], machine["name"]
    if not machine["online"]:
        raise Problem(f"「{name}」现在离线。")
    try:
        status, _, data = call(mid, method, route, body)
    except Offline:
        raise Problem(f"「{name}」现在离线，等它连上再试。") from None
    detail = json.loads(data or b"{}")
    if status == 404:
        raise Problem(f"请先升级「{name}」上的 ccim。")
    if status != 200:
        raise Problem(detail.get("error") or "操作失败，请重试。")
    return detail


def _device_call(call, machine, op, **body):
    return _peer_call(call, machine, "POST", API, json.dumps(dict(body, op=op), ensure_ascii=False).encode())


def _migrate(file, state, machines, call):
    """迁到第一次选中的文件夹。各台电脑完成后单独记下，离线的回来还能继续。"""
    migration = state["migration"]
    target = migration["target"]
    remaining = migration["remaining"]
    aliases = state.setdefault("aliases", {})
    imported = 0
    for source in list(remaining):
        try:
            if source not in machines or target["machine"] not in machines:
                raise Problem("原电脑或迁移目标已移除。")
            old = _peer_call(call, machines[source], "GET", "/api/space").get("notes", [])
            for start in range(0, len(old), 20):
                result = _device_call(call, machines[target["machine"]], "import",
                                      vault=target["id"], source=source, notes=old[start:start + 20])
                aliases.update(result["aliases"])
                imported += result["created"]
        except (OSError, ValueError) as e:
            remaining[source] = str(e)
        else:
            del remaining[source]
        _save(file, state)
    return imported


def select(acct, args, machines, call):
    """在网页入口统一记住最近的文件夹，并完成第一次迁移。"""
    machines = {m["id"]: m for m in machines}
    file = _state(acct, "recent")
    with PREF_LOCKS[acct]:
        state = _json(file)
        if args.get("retry") is True:
            imported = _migrate(file, state, machines, call) if state.get("migration") else 0
            return dict(_public(state), imported=imported)
        recent = state.get("recent", [])
        mid = args.get("machine")
        if not isinstance(mid, str) or mid not in machines:
            raise Problem("请选择一台电脑。")
        identifier = args.get("vault")
        if identifier:
            entry = next((x for x in recent if (x["machine"], x["id"]) == (mid, identifier)), None)
            if not entry:
                raise Problem("没有这个笔记文件夹。")
        else:
            entry = dict(_device_call(call, machines[mid], "register", path=args.get("path")), machine=mid)
        state["recent"] = ([entry] + [x for x in recent if x["id"] != entry["id"]])[:8]
        first = "migration" not in state
        if first:
            state["migration"] = {"target": entry, "remaining": dict.fromkeys(machines, "")}
        _save(file, state)
        imported = _migrate(file, state, machines, call) if first else 0
        return dict(_public(state), imported=imported)

def _relative(value, *, hidden=False):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise Problem("文件路径无效。")
    parts = value.split("/") if value else []
    if any(not p or p in (".", "..") or (not hidden and p.startswith(".")) for p in parts):
        raise Problem("只能打开笔记文件夹里的文件。")
    return value


def _path(root, rel, *, hidden=False):
    rel = _relative(rel, hidden=hidden)
    path = root
    for part in rel.split("/") if rel else ():
        path = path / part
        if path.is_symlink():
            raise Problem("笔记文件夹不跟随符号链接。")
    return path


def _root(value, top=None):
    if not isinstance(value, str) or not value:
        raise Problem("请选择笔记文件夹。")
    path = Path(value).expanduser().resolve()
    if top is not None and not path.is_relative_to(top):
        raise Problem("请选择自己的共享文件夹内的位置。")
    if path == Path(path.anchor) or not path.is_dir():
        raise Problem("找不到这个笔记文件夹。")
    return path


def _note_path(root, rel):
    path = _path(root, rel)
    if path.suffix.lower() != ".md":
        raise Problem("请选择 Markdown 笔记。")
    return path


def _read(path, limit):
    if not path.is_file():
        raise Problem("找不到这个文件。")
    with path.open("rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Problem("这个文件太大，无法在这里打开。")
    return data


def _note(root, path):
    data = _read(path, NOTE_MAX)
    return {"path": path.relative_to(root).as_posix(), "body": data.decode("utf-8-sig"),
            "version": hashlib.sha256(data).hexdigest()}


def _row(root, entry, directory):
    return {"path": entry.relative_to(root).as_posix(), "name": entry.name, "dir": directory,
            "mtime": entry.stat().st_mtime}


def _list(root, folder):
    if not folder.is_dir():
        raise Problem("找不到这个文件夹。")
    rows = []
    for p in folder.iterdir():
        if p.name.startswith(".") or p.is_symlink():
            continue
        directory = p.is_dir()
        if directory or p.suffix.lower() == ".md" and p.is_file():
            try:
                rows.append(_row(root, p, directory))
            except FileNotFoundError:
                continue
    return {"entries": sorted(rows, key=lambda x: (not x["dir"], x["name"].casefold()))}


def _files(root, kind):
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in workspace.SKIP)
        for name in sorted(names):
            p = Path(folder) / name
            if not name.startswith(".") and not p.is_symlink() and kind(p) and p.is_file():
                yield p


def _search(root, query, names_only=False):
    query = str(query or "").strip().casefold()
    rows = []
    for path in _files(root, lambda p: p.suffix.lower() == ".md"):
        if query not in path.relative_to(root).as_posix().casefold():
            if names_only:
                continue
            try:
                if query not in _read(path, NOTE_MAX).decode("utf-8").casefold():
                    continue
            except (Problem, OSError, UnicodeError):
                continue
        try:
            rows.append(_row(root, path, False))
        except FileNotFoundError:
            continue
        if len(rows) > 100:
            break
    return {"entries": rows[:100], "more": len(rows) > 100}


def _name(value, note=False):
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name.encode()) > 240 or name.startswith(".") or re.search(r'[\x00-\x1f<>:"/\\|?*]', name):
        raise Problem("请填写有效的文件名。")
    return name + ".md" if note and not name.lower().endswith(".md") else name


def _create(path, body=b""):
    try:
        _write(path, body, create=True)
    except FileExistsError:
        raise Problem("已经有同名文件了。") from None


def _find(root, ref, note, image=False, wiki=False):
    """按同目录、根目录、唯一短名称的顺序定位链接。"""
    ref = unquote(ref.split("#", 1)[0]).strip() if image and not wiki else ref.strip()
    if not ref or "\\" in ref or ref.startswith("//") or re.match(r"^[\w+.-]+:", ref):
        return None
    if not image and not ref.lower().endswith(".md"):
        ref += ".md"
    kind = (lambda p: p.suffix.lower().lstrip(".") in IMAGES) if image else (lambda p: p.suffix.lower() == ".md")
    normalized = ref.lstrip("/")
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(note), normalized))
    if ref.startswith("/"):
        candidates = [normalized]
    elif ref.startswith(("./", "../")):
        candidates = [relative]
    elif "/" in ref and (wiki or not image):
        candidates = [normalized, relative]
    else:
        candidates = [relative, normalized]
    for rel in candidates:
        if rel.startswith("../") or rel == "..":
            continue
        try:
            path = _path(root, rel)
        except Problem:
            continue
        if kind(path) and path.is_file():
            return path
    if "/" not in ref:
        matches = [p for p in _files(root, kind) if p.name.casefold() == ref.casefold()]
        if len(matches) > 1 and not image:
            raise Problem("有多篇同名笔记，请使用带文件夹的链接。")
        if len(matches) == 1:
            return matches[0]
    return None


def _settings(root, name):
    path = _path(root, ".obsidian/" + name + ".json", hidden=True)
    return _json(path)


def _daily(root):
    d = _settings(root, "daily-notes")
    fmt = d.get("format") or "YYYY-MM-DD"
    templates = _settings(root, "templates")
    return {"format": fmt, "date": templates.get("dateFormat") or fmt,
            "time": templates.get("timeFormat") or "HH:mm"}


def _daily_open(root, body):
    d = _settings(root, "daily-notes")
    folder = str(d.get("folder") or "").strip("/")
    stem = _relative(body.get("name"))
    for part in stem.split("/"):
        _name(part)
    path = _note_path(root, posixpath.join(folder, stem + ".md"))
    if not path.exists():
        template = str(d.get("template") or "").strip("/")
        text = ""
        if template:
            if not template.lower().endswith(".md"):
                template += ".md"
            p = _note_path(root, template)
            if not p.is_file():
                raise Problem("找不到 Obsidian 设置的日记模板。")
            text = _read(p, NOTE_MAX).decode("utf-8-sig")
        values = body.get("values") or {}
        if not isinstance(values, dict):
            raise Problem("模板变量无效。")
        def substitute(match):
            kind = match.group(1)
            return path.stem if kind == "title" else str(values.get(match[0], match[0]))
        tokens = sorted(set(m.group(0) for m in TEMPLATE.finditer(text) if m.group(1) != "title"))
        if any(t not in values for t in tokens):
            return {"tokens": tokens}
        data = TEMPLATE.sub(substitute, text).encode()
        if len(data) > NOTE_MAX:
            raise Problem("日记不能超过 4 MB。")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write(path, data, create=True)
        except FileExistsError:
            pass
    return _note(root, path)


TEMPLATE = re.compile(r"\{\{\s*(title|date|time)(?::([^{}]+))?\s*\}\}")


# ---------- 笔记流（#597）：日记拆成一条条，别的笔记算长文；顶上的输入框追加到今天的日记 ----------
FEED_MAX = 300            # 最多看最近改过的多少个文件
ENTRY_MAX = 4000          # 一条记录最多给多少字
# 日记里一条的开头，和 ccim 的「记一下」（diary.py）同一种写法；Obsidian 里引用块包着的也认
ENTRY_HEAD = re.compile(r"^>?\s*(?:## (\d{1,2}:\d{2})(?: · (.*))?|\*\*(\d{1,2}:\d{2})\*\*(?: · 来自 (.*))?)\s*$")
ENTRY_LINK = re.compile(r"^\[(.*)\]\((\S+)\)$")
DATE_TOKENS = {"YYYY": r"(?P<y>\d{4})", "MM": r"(?P<m>\d{2})", "DD": r"(?P<d>\d{2})",
               "M": r"(?P<m>\d{1,2})", "D": r"(?P<d>\d{1,2})"}


def _date_pattern(fmt):
    """Obsidian 的日记文件名格式（moment 写法）→ 正则；认不出的格式退回 年-月-日。"""
    out, i = "", 0
    while i < len(fmt):
        if fmt[i] == "[":
            j = fmt.find("]", i)
            if j < 0:
                break
            out += re.escape(fmt[i + 1:j]); i = j + 1; continue
        tok = next((t for t in ("YYYY", "MM", "DD", "M", "D") if fmt.startswith(t, i)), None)
        if tok:
            if f"(?P<{tok[0].lower()}>" in out:
                return None
            out += DATE_TOKENS[tok]; i += len(tok); continue
        if fmt[i].isascii() and fmt[i].isalpha():   # 认不出的日期写法（周几、季度等）
            return None
        out += re.escape(fmt[i]); i += 1
    return re.compile(out + "$") if all(f"(?P<{k}>" in out for k in "ymd") else None


def _entries(text):
    """日记正文 → [{time, where, link, text}]。第一条开头之前写的算一条没有时间的。"""
    out, cur = [], {"time": "", "where": "", "link": "", "lines": []}
    for ln in text.replace("\r", "").split("\n"):
        m = ENTRY_HEAD.match(ln)
        if m:
            out.append(cur)
            where = (m.group(2) or m.group(4) or "").strip()
            lk = ENTRY_LINK.match(where)
            cur = {"time": m.group(1) or m.group(3), "where": lk.group(1).strip("「」") if lk else where,
                   "link": lk.group(2) if lk else "", "lines": []}
        else:
            cur["lines"].append(ln)
    out.append(cur)
    rows = []
    for e in out:
        body = "\n".join(e.pop("lines")).strip()
        if not body and not e["time"]:
            continue
        e["text"] = body[:ENTRY_MAX]
        rows.append(e)
    return rows


def _plain(text, n):
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)|!\[\[[^\]]*\]\]", "", t)
    t = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s*(#{1,6}\s|[-*+]\s(\[.\]\s)?|\d+\.\s|>\s?)", "", t, flags=re.M)
    t = re.sub(r"[*_`~]", "", t)
    t = " ".join(t.split())
    return t[:n]


def _feed(root):
    """最近改过的笔记：日记（文件名是日期的）拆成一条条记录，别的是长文（标题 + 摘要）。"""
    pattern = _date_pattern(_daily(root)["format"]) or _date_pattern("YYYY-MM-DD")
    files = []
    for path in _files(root, lambda p: p.suffix.lower() == ".md"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    files.sort(key=lambda x: -x[0])
    rows = []
    for mtime, path in files[:FEED_MAX]:
        try:
            text = _read(path, NOTE_MAX).decode("utf-8-sig")
        except (Problem, OSError, UnicodeError):
            continue
        rel = path.relative_to(root).as_posix()
        m = pattern.match(path.stem) or _date_pattern("YYYY-MM-DD").match(path.stem)
        if m:
            rows.append({"path": rel, "kind": "daily", "mtime": mtime,
                         "date": f"{int(m['y']):04d}-{int(m['m']):02d}-{int(m['d']):02d}", "entries": _entries(text)})
            continue
        head = re.search(r"^#\s+(.+)$", text, re.M)
        body = text[head.end():] if head and not text[:head.start()].strip() else text
        rows.append({"path": rel, "kind": "note", "mtime": mtime, "title": head.group(1).strip() if head else path.stem,
                     "excerpt": _plain(body, 160), "chars": len(re.sub(r"\s", "", text))})
    return {"items": rows, "more": len(files) > FEED_MAX}


def _append(root, body):
    """顶上的输入框记一笔（#597）：追加到今天的日记末尾，开头一行是时间，和「记一下」一样。"""
    text = body.get("text")
    when = body.get("time")
    if not isinstance(text, str) or not text.strip():
        raise Problem("先写点什么。")
    if not isinstance(when, str) or not re.fullmatch(r"\d{1,2}:\d{2}", when):
        raise Problem("时间无效。")
    note = _daily_open(root, body)
    if "tokens" in note:
        return note
    path = _note_path(root, note["path"])
    old = _read(path, NOTE_MAX).decode("utf-8-sig")
    block = f"**{when}**\n\n{text.strip()}\n"
    sep = "" if not old.strip() else "\n" if old.endswith("\n") else "\n\n"
    data = (old + sep + block).encode()
    if len(data) > NOTE_MAX:
        raise Problem("日记不能超过 4 MB。")
    _write(path, data)
    return _note(root, path)


def _attach(root, body):
    note = _note_path(root, body.get("path"))
    if not note.is_file():
        raise Problem("找不到要插入图片的笔记。")
    raw = body.get("data")
    if not isinstance(raw, str) or len(raw) > (IMAGE_MAX + 2) // 3 * 4:
        raise Problem("图片不能超过 10 MB。")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise Problem("图片数据无效。") from None
    if not data or len(data) > IMAGE_MAX:
        raise Problem("图片为空或超过 10 MB。")
    name = _name(body.get("name"))
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in IMAGES:
        raise Problem("请选择支持的图片格式。")
    obsidian = _path(root, ".obsidian", hidden=True).is_dir()
    app = _settings(root, "app") if obsidian else {}
    where = str(app.get("attachmentFolderPath") or ("" if obsidian else "附件"))
    if where == "." or where.startswith("./"):
        parent = note.parent.relative_to(root).as_posix() if note.parent != root else ""
        folder = posixpath.join(parent, where[2:]).rstrip("/")
    else:
        folder = where.strip("/")
    directory = _path(root, folder)
    directory.mkdir(parents=True, exist_ok=True)
    path = Path(workspace._unique(directory, name))
    _create(path, data)
    return {"path": path.relative_to(root).as_posix(), "relative": posixpath.relpath(path, note.parent)}


def _import(root, file, state, item, source, identifier):
    old_id = item.get("id")
    if not isinstance(old_id, str) or not re.fullmatch(r"[0-9a-f]{10}", old_id):
        raise Problem("旧笔记编号无效。")
    key = f"{identifier}:{source}:{old_id}"
    records = state.setdefault("imports", {})
    record = records.get(key)
    if record and record["done"]:
        return {"path": record["path"]}
    title = (item.get("title") or "").strip()
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', " ", title).strip(" .") or "未命名笔记"
    title = title.encode()[:225].decode("utf-8", "ignore")
    data = (item.get("body") or "").encode()
    if len(data) > NOTE_MAX:
        raise Problem("旧笔记太大。")
    digest = hashlib.sha256(data).hexdigest()
    path = _note_path(root, record["path"]) if record else None
    if not record or (path.exists() and hashlib.sha256(_read(path, NOTE_MAX)).hexdigest() != record["digest"]):
        path = Path(workspace._unique(root, _name(title, True)))
        record = records[key] = {"path": path.name, "digest": digest, "done": False}
        _save(file, state)
    if not path.exists():
        _create(path, data)
    record["done"] = True
    _save(file, state)
    return {"path": record["path"], "created": True}


def device(acct, top, args):
    op = args.get("op")
    file = _state(acct, "device")
    with DEVICE_LOCK if op in DEVICE_WRITES else nullcontext():
        state = _json(file)
        roots = state.setdefault("roots", {})
        if op == "register":
            root = _root(args.get("path"), top)
            current = str(root)
            identifier = next((k for k, v in roots.items() if v == current), None)
            if not identifier:
                identifier = secrets.token_hex(12)
                roots[identifier] = current
                _save(file, state)
            return {"id": identifier, "name": root.name, "path": current}
        identifier = args.get("vault")
        if not isinstance(identifier, str) or identifier not in roots:
            raise Problem("找不到这个笔记文件夹。")
        stored = roots[identifier]
        root = _root(stored, top)
        if str(root) != stored:
            raise Problem("笔记文件夹的位置已改变，请重新选择。")
        rel = args.get("path", "")
        if op == "import":
            notes = args.get("notes")
            source = args.get("source")
            if not isinstance(notes, list) or len(notes) > 20 or not isinstance(source, str):
                raise Problem("旧笔记数据无效。")
            aliases, created = {}, 0
            for note in notes:
                if not isinstance(note, dict):
                    raise Problem("旧笔记数据无效。")
                result = _import(root, file, state, note, source, identifier)
                aliases[note["id"]] = result["path"]
                created += int(result.get("created", False))
            return {"aliases": aliases, "created": created}
        if op == "list":
            return _list(root, _path(root, rel))
        if op in ("search", "suggest"):
            return _search(root, args.get("query"), op == "suggest")
        if op == "read":
            return _note(root, _note_path(root, rel))
        if op == "save":
            path = _note_path(root, rel)
            data = _read(path, NOTE_MAX)
            text = args.get("body")
            if not isinstance(text, str):
                raise Problem("笔记正文无效。")
            bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
            if b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""):
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            updated = bom + text.encode()
            if len(updated) > NOTE_MAX:
                raise Problem("笔记不能超过 4 MB。")
            version = hashlib.sha256(updated).hexdigest()
            if data != updated:
                if args.get("version") != hashlib.sha256(data).hexdigest():
                    return {"conflict": True}
                _write(path, updated)
            return {"version": version}
        if op == "create":
            folder = _path(root, rel)
            directory = args.get("folder") is True
            path = folder / _name(args.get("name"), not directory)
            if directory:
                try:
                    path.mkdir()
                except FileExistsError:
                    raise Problem("已经有同名文件夹了。") from None
            else:
                _create(path)
            return {"path": path.relative_to(root).as_posix()}
        if op in ("rename", "trash"):
            path = _path(root, rel)
            if path == root or not (path.is_dir() or path.suffix.lower() == ".md" and path.is_file()):
                raise Problem("找不到要操作的笔记或子文件夹。")
            if op == "trash":
                devices._to_trash(str(path))
                return {}
            new = path.with_name(_name(args.get("name"), not path.is_dir()))
            if new != path:
                if os.path.lexists(new) and (new.is_symlink() or new.name.casefold() != path.name.casefold()
                                              or not new.samefile(path)):
                    raise Problem("已经有同名文件了。")
                path.rename(new)
            return {"path": new.relative_to(root).as_posix()}
        if op == "daily":
            return _daily(root)
        if op == "daily_open":
            return _daily_open(root, args)
        if op == "feed":
            return _feed(root)
        if op == "append":
            return _append(root, args)
        if op == "link":
            _note_path(root, rel)
            ref = str(args.get("ref") or "").split("|", 1)[0].split("#", 1)[0].strip()
            if not ref:
                return {"path": rel}
            path = _find(root, ref, rel)
            if not path:
                target = ref.lstrip("/")
                if ref.startswith(("./", "../")):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), ref))
                folder, _, name = target.rpartition("/")
                dest = _path(root, folder)
                dest.mkdir(parents=True, exist_ok=True)
                path = dest / _name(name, True)
                _create(path)
            return {"path": path.relative_to(root).as_posix()}
        if op == "attach":
            return _attach(root, args)
        if op == "asset":
            _note_path(root, rel)
            path = _find(root, str(args.get("ref") or ""), rel, image=True, wiki=bool(args.get("wiki")))
            if not path:
                raise Problem("找不到这张图片。")
            return {"type": IMAGES[path.suffix.lower()[1:]], "data": base64.b64encode(_read(path, IMAGE_MAX)).decode()}
        raise Problem("找不到这个笔记操作。")
