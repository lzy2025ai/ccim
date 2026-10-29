"""工作区（#206）：对话的工作目录当成文件树，在网页里看、新建、改名、移动、删除。

范围是这个对话的工作目录（realpath 后比较，软链接指到外面的不跟过去）。删除一律移到废纸篓，返回一个能撤销的记号。
AI 这一轮改了哪些文件：编辑工具写的（会话里记下的）加上目录里修改时间晚于这一轮开始的（命令行、Codex 改的也算），
扫描有数量和时间上限，主目录这种太大的目录不扫。
"""
import os, shutil, subprocess, time

from . import docs

HOME = os.path.realpath(os.path.expanduser("~"))
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".DS_Store", ".idea", ".next", ".cache", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "dist", ".turbo"}
LIST_MAX = 2000
SCAN_FILES = 20000
SCAN_SECONDS = 0.8
TRASH = os.path.join(HOME, ".Trash")


class Problem(Exception):
    pass


def _inside(root, p):
    return p == root or p.startswith(root + os.sep)


def resolve(root, rel=""):
    """工作目录里的一个路径（相对或绝对都行），出了范围就拒绝。"""
    root = os.path.realpath(root)
    rel = rel or ""
    p = os.path.realpath(rel if os.path.isabs(rel) else os.path.join(root, rel))
    if not _inside(root, p):
        raise Problem("只能管理这个对话工作目录里的文件。")
    return p


def entry_path(root, rel):
    """改名、移动、删除用：最后一段不跟软链接（动的是链接本身），上级目录要在范围里。"""
    root = os.path.realpath(root)
    full = rel if os.path.isabs(rel or "") else os.path.join(root, rel or "")
    full = os.path.normpath(full)
    if full == root:
        return root
    p = os.path.join(resolve(root, os.path.dirname(full)), os.path.basename(full))
    if not _inside(root, p):
        raise Problem("只能管理这个对话工作目录里的文件。")
    return p


def _entry(root, d, n):
    p = os.path.join(d, n)
    try:
        st = os.stat(p)
    except OSError:
        return None
    isdir = os.path.isdir(p)
    if os.path.islink(p) and not _inside(root, os.path.realpath(p)):
        return None                       # 指到工作目录外面的软链接不列
    return {"name": n, "path": p, "dir": isdir, "size": None if isdir else st.st_size, "mtime": st.st_mtime}


def ls(root, rel="", hidden=False):
    root = os.path.realpath(root)
    d = resolve(root, rel)
    if not os.path.isdir(d):
        raise Problem("找不到这个文件夹。")
    try:
        names = os.listdir(d)
    except PermissionError:
        raise Problem("没有权限打开这个文件夹。")
    rows = []
    for n in names:
        if not hidden and (n.startswith(".") or n in SKIP):
            continue
        e = _entry(root, d, n)
        if e:
            rows.append(e)
    rows.sort(key=lambda r: (not r["dir"], r["name"].lower()))
    return {"root": root, "path": d, "entries": rows[:LIST_MAX], "more": max(0, len(rows) - LIST_MAX)}


def _name_ok(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise Problem("名字不能为空，也不能有 /。")
    return name


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    q, i = os.path.join(folder, name), 2
    while os.path.lexists(q):
        q = os.path.join(folder, f"{base} {i}{ext}")
        i += 1
    return q


def create(root, rel, name, folder=False):
    d = resolve(root, rel)
    if not os.path.isdir(d):
        raise Problem("找不到这个文件夹。")
    p = _unique(d, _name_ok(name))
    if folder:
        os.mkdir(p)
    else:
        with open(p, "x", encoding="utf-8") as f:
            if p.lower().endswith((".md", ".markdown")):
                f.write(f"# {os.path.splitext(os.path.basename(p))[0]}\n\n")
    return {"path": p}


def _movable(root, p):
    if p == os.path.realpath(root):
        raise Problem("工作目录本身不能改名、移动或删除。")
    if not os.path.lexists(p):
        raise Problem("已经不在了。")


def rename(root, rel, name):
    p = entry_path(root, rel)
    _movable(root, p)
    q = os.path.join(os.path.dirname(p), _name_ok(name))
    if q == p:
        return {"path": p}
    if os.path.lexists(q):
        raise Problem("已经有同名的了。")
    os.rename(p, q)
    return {"path": q, "from": p}


def move(root, rel, to):
    p, d = entry_path(root, rel), resolve(root, to)
    _movable(root, p)
    if not os.path.isdir(d):
        raise Problem("找不到要移到的文件夹。")
    if _inside(p, d):
        raise Problem("不能移到它自己里面。")
    if os.path.dirname(p) == d:
        return {"path": p}
    q = _unique(d, os.path.basename(p))
    shutil.move(p, q)
    return {"path": q, "from": p}


def trash(root, rel):
    """移到废纸篓，返回撤销用的记号（废纸篓里的位置 + 原来的位置）。"""
    p = entry_path(root, rel)
    _movable(root, p)
    os.makedirs(TRASH, exist_ok=True)
    q = _unique(TRASH, os.path.basename(p))
    try:
        os.rename(p, q)
    except OSError:                       # 不在同一个卷上
        shutil.move(p, q)
    return {"path": p, "token": q}


def restore(root, token, path):
    """撤销删除：从废纸篓搬回原处（原处又有了同名的就换个名字）。"""
    t = os.path.realpath(token or "")
    if os.path.dirname(t) != os.path.realpath(TRASH) or not os.path.lexists(t):
        raise Problem("废纸篓里已经找不到它了。")
    dest = resolve(root, path)
    if not os.path.isdir(os.path.dirname(dest)):
        raise Problem("原来的文件夹已经不在了。")
    q = _unique(os.path.dirname(dest), os.path.basename(dest))
    shutil.move(t, q)
    return {"path": q}


def put(root, uploads, key, rel, name):
    """网页传上来的文件（/api/upload 存在 uploads 里）放进这个文件夹。"""
    d = resolve(root, rel)
    up = os.path.realpath(uploads)
    k = os.path.realpath(key or "")
    if not k.startswith(up + os.sep) or not os.path.isfile(k):
        raise Problem("文件没传上来，再试一次。")
    if not os.path.isdir(d):
        raise Problem("找不到这个文件夹。")
    q = _unique(d, _name_ok(name or os.path.basename(k)))
    shutil.move(k, q)
    return {"path": q}


def paths(root, items):
    """回复里写到的路径（#296）：哪些在工作目录里、确实存在。返回 {原样写的: {path, dir, view}}（view：网页里能预览），不在的不返回。"""
    root = os.path.realpath(root)
    out = {}
    for rel in list(items or [])[:200]:
        if not isinstance(rel, str) or not rel.strip() or rel in out:
            continue
        try:
            p = resolve(root, os.path.expanduser(rel))
        except (Problem, OSError, ValueError):
            continue
        if p != root and os.path.exists(p):
            d = os.path.isdir(p)
            out[rel] = {"path": p, "dir": d, "view": not d and bool(docs.kind(p))}
    return {"paths": out}


def reveal(root, rel):
    """在访达里选中它（#296）。只在这台电脑上的网页能用。"""
    p = resolve(root, os.path.expanduser(rel or ""))
    if not os.path.exists(p):
        raise Problem("找不到这个文件了。")
    subprocess.run(["open", "-R", p], capture_output=True)


def changed_since(root, t0, known=(), skip=None, created=()):
    """这一轮改过的文件：[{path, new}]。known 是编辑工具写过的（created 是其中新建的）；再扫一遍目录找修改时间晚于 t0 的。
    skip(path, mtime) 为真的不算（用户自己在网页里保存的）。"""
    root = os.path.realpath(root)
    out, seen = [], set()
    made = {os.path.realpath(p) for p in created or ()}
    tool = {os.path.realpath(p) for p in known or ()}

    def add(p, st):
        if p in seen or (skip and skip(p, st.st_mtime)):
            return
        seen.add(p)
        if p in tool:
            new = p in made
        else:                             # 命令行、Codex 改的：看文件是什么时候建的（整个换掉的文件也会算成新建）
            born = getattr(st, "st_birthtime", None)
            new = bool(born and born >= t0 - 1)
        out.append({"path": p, "new": new})

    for p in known or ():
        try:
            rp = os.path.realpath(p)
            st = os.stat(rp)
        except OSError:
            continue
        if os.path.isfile(rp) and st.st_mtime >= t0 - 1:
            add(rp, st)
    if root not in (HOME, "/"):           # 主目录太大，只认编辑工具写过的
        deadline, n = time.monotonic() + SCAN_SECONDS, 0
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith(".")]
            for f in files:
                n += 1
                if n > SCAN_FILES or time.monotonic() > deadline:
                    break
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime >= t0:
                    add(p, st)
            else:
                continue
            break
    out.sort(key=lambda r: r["path"])
    return out[:60]
