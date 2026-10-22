"""平台 Skill（#101）：在平台上装一份，各台电脑上的 Agent 通过 ccim 对话时都能用；各台电脑本地自己装的照常可用。

存在 ~/.ccim/skills/ 下：lib/<名字>/ 是 Skill 本身（SKILL.md 和附带的文件），meta/<名字>.json 是来源、说明、修改时间、停用，
删掉的记在 _tombstones.json。会话启动时按 Agent 生成一个本地插件 views/<Agent>/（.claude-plugin/plugin.json + skills/ 下软链到 lib），
用 SDK 的 plugins 挂上去，Claude 里显示成「platform:名字」，不会和本地同名的 Skill 冲突。只有 Claude Code 能挂。

同步和知识库一样：每台电脑一份完整的，定期问在线的其他电脑要清单（每个 Skill 的修改时间、删除记录），比自己新的整个拉过来
（打包成 tar.gz、base64），后改的算数；服务器只转发。
"""
import base64, io, json, logging, os, re, shutil, tarfile, tempfile, threading, time, urllib.request, zipfile

from . import registry

log = logging.getLogger("ccim.skills")
LOCK = threading.RLock()
PLUGIN = "platform"                   # 插件名：Claude 里显示成 platform:名字
MAX = 20 << 20                        # 一个 Skill 最多这么大
SKIP = {".git", "node_modules", "__pycache__", ".DS_Store"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class Problem(Exception):
    pass


def root():
    return os.path.join(registry.HOME, "skills")


def _lib(name=""):
    return os.path.join(root(), "lib", name) if name else os.path.join(root(), "lib")


def _meta_path(name):
    return os.path.join(root(), "meta", name + ".json")


def _check(name):
    if not NAME.fullmatch(name or ""):
        raise Problem("找不到这个 Skill。")
    return name


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


def _read_meta(name):
    try:
        with open(_meta_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _front(text):
    """SKILL.md 开头 --- 之间的 name、description。"""
    m = re.match(r"\s*---\s*\n(.*?)\n---", text or "", re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description") and v.strip():
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _size(d):
    n = 0
    for dp, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP]
        n += sum(os.path.getsize(os.path.join(dp, f)) for f in files if not os.path.islink(os.path.join(dp, f)))
    return n


# ----- 读 -----

def listing():
    out = []
    if os.path.isdir(_lib()):
        for name in sorted(os.listdir(_lib()), key=str.lower):
            m = _read_meta(name)
            if m and os.path.isfile(os.path.join(_lib(name), "SKILL.md")):
                out.append(m)
    return out


def detail(name):
    m = _read_meta(_check(name))
    if not m:
        raise Problem("找不到这个 Skill。")
    with open(os.path.join(_lib(name), "SKILL.md"), encoding="utf-8", errors="replace") as f:
        text = f.read()
    files = []
    for dp, dirs, fs in os.walk(_lib(name)):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        files += [os.path.relpath(os.path.join(dp, f), _lib(name)) for f in sorted(fs) if f not in SKIP]
    return {**m, "text": text, "files": files[:2000]}


IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
          ".svg": "image/svg+xml"}


def _file(name, rel):
    base = os.path.realpath(_lib(_check(name)))
    p = os.path.realpath(os.path.join(base, rel or ""))
    if not p.startswith(base + os.sep) or not os.path.isfile(p):
        raise Problem("找不到这个文件。")
    return p


def read_file(name, rel):
    """Skill 里的一个文件：文字给原文，图片给 data 地址，其他的只给大小。"""
    import base64 as b64
    p = _file(name, rel)
    st = os.stat(p)
    out = {"path": rel, "size": st.st_size, "mtime": st.st_mtime}
    ext = os.path.splitext(p)[1].lower()
    if ext in IMAGES and st.st_size <= 5 << 20:
        with open(p, "rb") as f:
            return {**out, "image": f"data:{IMAGES[ext]};base64," + b64.b64encode(f.read()).decode()}
    if st.st_size > 2 << 20:
        return {**out, "binary": True}
    with open(p, "rb") as f:
        data = f.read()
    try:
        return {**out, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {**out, "binary": True}


def save_file(name, rel, text, mtime):
    """在网页里改了 Skill 的文件：打开以后被改过就不写；记成这个 Skill 改过，同步到别的电脑。"""
    if not isinstance(text, str):
        raise Problem("内容不对。")
    with LOCK:
        p = _file(name, rel)
        if mtime and abs(os.path.getmtime(p) - float(mtime)) > 0.001:
            raise Problem("这个文件在你打开以后被改过了，刷新后再改。")
        _write(p, text)
        m = _read_meta(name) or {}
        if rel == "SKILL.md":
            m["desc"] = _front(text).get("description", m.get("desc", ""))
        m.update(updated=time.time(), size=_size(_lib(name)))
        _write(_meta_path(name), json.dumps(m, ensure_ascii=False))
    return read_file(name, rel)


def local_list():
    """这台电脑上 Claude Code 自己装的 Skill（~/.claude/skills），可以导入平台。"""
    d = os.path.expanduser("~/.claude/skills")
    have = {m["name"] for m in listing()}
    out = []
    for n in sorted(os.listdir(d) if os.path.isdir(d) else [], key=str.lower):
        p = os.path.join(d, n, "SKILL.md")
        if not NAME.fullmatch(n) or not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            fm = _front(f.read(4000))
        out.append({"name": n, "desc": fm.get("description", ""), "installed": n in have})
    return out


# ----- 装、改、删 -----

def _put(src, source, name=None):
    """把 src 目录装成名字为 name 的 Skill（已有就换掉）。"""
    with open(os.path.join(src, "SKILL.md"), encoding="utf-8", errors="replace") as f:
        fm = _front(f.read())
    name = name or (fm.get("name") if NAME.fullmatch(fm.get("name") or "") else None) or os.path.basename(src.rstrip("/"))
    if not NAME.fullmatch(name or ""):
        raise Problem(f"「{name}」不能当 Skill 的名字，只能用英文字母、数字、点、横线、下划线。")
    if _size(src) > MAX:
        raise Problem(f"「{name}」太大了（最多 {MAX >> 20} MB）。")
    with LOCK:
        tmp = _lib(f".{name}.new")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, symlinks=False, ignore=shutil.ignore_patterns(*SKIP))
        old = _read_meta(name) or {}
        dest = _lib(name)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        meta = {"name": name, "desc": fm.get("description", ""), "source": source, "updated": time.time(),
                "installed": old.get("installed") or time.time(), "disabled": old.get("disabled", False), "size": _size(dest)}
        _write(_meta_path(name), json.dumps(meta, ensure_ascii=False))
        t = _tombs()
        if name in t:
            t.pop(name)
            _write(os.path.join(root(), "_tombstones.json"), json.dumps(t))
    return name


def _found(base):
    """base 下面所有带 SKILL.md 的文件夹（不再往里找）。"""
    hits = []
    for dp, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x not in SKIP and not x.startswith("."))
        if "SKILL.md" in files:
            hits.append(dp)
            dirs[:] = []
    return hits


def _safe_extract(blob, kind, dest):
    """解压到 dest，不许写到外面去。"""
    real = os.path.realpath(dest)
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if not os.path.realpath(os.path.join(dest, n)).startswith(real + os.sep):
                    raise Problem("压缩包里的路径不对。")
            z.extractall(dest)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as t:
            members = []
            for m in t.getmembers():
                if not (m.isfile() or m.isdir()):
                    continue                  # 软链、设备文件一律不要
                if not os.path.realpath(os.path.join(dest, m.name)).startswith(real + os.sep):
                    raise Problem("压缩包里的路径不对。")
                members.append(m)
            t.extractall(dest, members=members)


def _install_dir(base, source):
    hits = _found(base)
    if not hits:
        raise Problem("里面没找到 Skill（要有 SKILL.md）。")
    return [_put(h, source) for h in hits]


def _github(url):
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?/?$", (url or "").strip())
    if not m:
        raise Problem("要 GitHub 上的地址，比如 https://github.com/用户/仓库 或者仓库里某个文件夹的地址。")
    owner, repo, ref, sub = m.groups()
    sub = re.sub(r"/?SKILL\.md$", "", sub or "")
    return owner, repo, ref or "HEAD", sub


def _download(url, limit=60 << 20):
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "ccim"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            data = r.read(limit + 1)
    except Exception as e:
        raise Problem(f"下载不下来：{e}")
    if len(data) > limit:
        raise Problem("仓库太大了。")
    return data


def install_github(url):
    owner, repo, ref, sub = _github(url)
    blob = _download(f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}")
    with tempfile.TemporaryDirectory() as tmp:
        _safe_extract(blob, "tar", tmp)
        tops = os.listdir(tmp)
        base = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
        base = os.path.join(base, sub) if sub else base
        if not os.path.realpath(base).startswith(os.path.realpath(tmp)) or not os.path.isdir(base):
            raise Problem("仓库里没有这个文件夹。")
        return _install_dir(base, {"type": "github", "url": url.strip()})


def install_upload(key, pdir, name):
    """网页传上来的压缩包（zip、.skill、tar.gz）或单个 SKILL.md。"""
    up = os.path.realpath(os.path.join(pdir, "uploads"))
    k = os.path.realpath(key or "")
    if not k.startswith(up + os.sep) or not os.path.isfile(k):
        raise Problem("文件没传上来，再试一次。")
    with open(k, "rb") as f:
        blob = f.read()
    os.remove(k)
    src = {"type": "upload", "file": name}
    with tempfile.TemporaryDirectory() as tmp:
        if (name or "").lower().endswith(".md"):
            d = os.path.join(tmp, re.sub(r"\.md$", "", os.path.basename(name), flags=re.I) or "skill")
            if d.endswith("SKILL"):
                d = os.path.join(tmp, "skill")
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "wb") as f:
                f.write(blob)
        else:
            kind = "zip" if blob[:2] == b"PK" else "tar"
            try:
                _safe_extract(blob, kind, tmp)
            except (zipfile.BadZipFile, tarfile.TarError):
                raise Problem("打不开这个文件：要 zip、.skill、tar.gz 压缩包，或者 SKILL.md。")
        return _install_dir(tmp, src)


def import_local(name, machine_name=""):
    d = os.path.realpath(os.path.join(os.path.expanduser("~/.claude/skills"), _check(name)))
    if not os.path.isfile(os.path.join(d, "SKILL.md")):
        raise Problem("这台电脑上没有这个 Skill。")
    return [_put(d, {"type": "local", "machine": machine_name}, name=name)]


def update(name):
    m = _read_meta(_check(name)) or {}
    src = m.get("source") or {}
    if src.get("type") != "github":
        raise Problem("只有从 GitHub 装的能在这里更新，其他的重新上传或导入就行。")
    owner, repo, ref, sub = _github(src["url"])
    blob = _download(f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}")
    with tempfile.TemporaryDirectory() as tmp:
        _safe_extract(blob, "tar", tmp)
        tops = os.listdir(tmp)
        base = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
        base = os.path.join(base, sub) if sub else base
        hit = next((h for h in _found(base) if os.path.basename(h) == name or _front(open(os.path.join(h, "SKILL.md"), encoding="utf-8", errors="replace").read()).get("name") == name), None)
        if not hit:
            raise Problem("仓库里已经找不到它了。")
        _put(hit, src, name=name)
    return {"name": name}


def set_disabled(name, off):
    with LOCK:
        m = _read_meta(_check(name))
        if not m:
            raise Problem("找不到这个 Skill。")
        m.update(disabled=bool(off), updated=time.time())
        _write(_meta_path(name), json.dumps(m, ensure_ascii=False))
    return m


def delete(name):
    with LOCK:
        _check(name)
        shutil.rmtree(_lib(name), ignore_errors=True)
        try:
            os.remove(_meta_path(name))
        except OSError:
            pass
        t = _tombs()
        t[name] = time.time()
        _write(os.path.join(root(), "_tombstones.json"), json.dumps(t))
    return {}


# ----- 挂到会话 -----

def plugin_for(agent=None):
    """给这个 Agent 的会话用的本地插件目录（只放没停用、它没去掉的 Skill）；一个都没有返回 None。"""
    off = set((agent or {}).get("skills_off") or [])
    names = [m["name"] for m in listing() if not m.get("disabled") and m["name"] not in off]
    if not names:
        return None
    key = re.sub(r"[^\w-]", "_", (agent or {}).get("id") or "all")
    view = os.path.join(root(), "views", key)
    with LOCK:
        try:                                  # 没变就不重建：同一个 Agent 的别的会话可能正在用它
            if sorted(os.listdir(os.path.join(view, "skills"))) == sorted(names):
                return view
        except OSError:
            pass
        shutil.rmtree(view, ignore_errors=True)
        os.makedirs(os.path.join(view, ".claude-plugin"))
        os.makedirs(os.path.join(view, "skills"))
        with open(os.path.join(view, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as f:
            json.dump({"name": PLUGIN, "description": "ccim 平台上装的 Skill", "version": "1.0.0"}, f, ensure_ascii=False)
        for n in names:
            os.symlink(_lib(n), os.path.join(view, "skills", n))
    return view


# ----- 同步 -----

def manifest():
    return {"items": {m["name"]: m.get("updated", 0) for m in listing()}, "tombs": _tombs()}


def export(keys):
    out = {}
    for name in keys or []:
        try:
            m = _read_meta(_check(name))
            if not m:
                continue
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(_lib(name), arcname=name, filter=lambda ti: None if os.path.basename(ti.name) in SKIP else ti)
            out[name] = {"meta": m, "tar": base64.b64encode(buf.getvalue()).decode()}
        except (OSError, Problem):
            continue
    return out


def wanted(remote):
    mine = manifest()
    tombs, items = mine["tombs"], mine["items"]
    pull = [k for k, t in (remote.get("items") or {}).items() if t > items.get(k, -1) and t > tombs.get(k, -1)]
    drop = [k for k, t in (remote.get("tombs") or {}).items() if k in items and t >= items[k]]
    return pull, drop, remote.get("tombs") or {}


def apply(contents, drop, tombs):
    changed = False
    for name, c in (contents or {}).items():
        try:
            _check(name)
            with tempfile.TemporaryDirectory() as tmp:
                _safe_extract(base64.b64decode(c["tar"]), "tar", tmp)
                src = os.path.join(tmp, name)
                with LOCK:
                    dest = _lib(name)
                    stage = _lib(f".{name}.new")
                    shutil.rmtree(stage, ignore_errors=True)
                    os.makedirs(_lib(), exist_ok=True)
                    shutil.move(src, stage)
                    if os.path.exists(dest):
                        shutil.rmtree(dest)
                    os.replace(stage, dest)
                    _write(_meta_path(name), json.dumps(c["meta"], ensure_ascii=False))
            changed = True
        except Exception:
            log.warning("同步过来的 Skill %s 装不上", name, exc_info=True)
    with LOCK:
        for name in drop or []:
            try:
                _check(name)
                shutil.rmtree(_lib(name), ignore_errors=True)
                os.remove(_meta_path(name))
                changed = True
            except (OSError, Problem):
                pass
        mine = _tombs()
        merged = {k: max(v, mine.get(k, 0)) for k, v in (tombs or {}).items()}
        if any(mine.get(k) != v for k, v in merged.items()):
            _write(os.path.join(root(), "_tombstones.json"), json.dumps({**mine, **merged}))
    return changed
