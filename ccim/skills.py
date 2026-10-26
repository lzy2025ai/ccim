"""平台 Skill（#101）：在平台上装一份，各台电脑上的 Agent 通过 ccim 对话时都能用；各台电脑本地自己装的照常可用。

存在 ~/.ccim/skills/ 下：lib/<名字>/ 是 Skill 本身（SKILL.md 和附带的文件），meta/<名字>.json 是来源、说明、修改时间、停用，
删掉的记在 _tombstones.json。会话启动时按 Agent 生成一个本地插件 views/<Agent>/（.claude-plugin/plugin.json + skills/ 下软链到 lib），
用 SDK 的 plugins 挂上去，Claude 里显示成「platform:名字」，不会和本地同名的 Skill 冲突。只有 Claude Code 能挂。

同步和知识库一样：每台电脑一份完整的，定期问在线的其他电脑要清单（每个 Skill 的修改时间、删除记录），比自己新的整个拉过来
（打包成 tar.gz、base64），后改的算数；服务器只转发。
"""
import base64, contextlib, contextvars, io, json, logging, os, re, shutil, tarfile, tempfile, threading, time, urllib.request, zipfile

from . import registry

log = logging.getLogger("ccim.skills")
LOCK = threading.RLock()
PLUGIN = "platform"                   # 插件名：Claude 里显示成 platform:名字
MAX = 20 << 20                        # 一个 Skill 最多这么大
SKIP = {".git", "node_modules", "__pycache__", ".DS_Store"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class Problem(Exception):
    pass


ON_CHANGE = []                        # 平台 Skill 改了以后要做的事（平台进程登记：叫别的电脑来同步、刷新网页）


def changed():
    for fn in ON_CHANGE:
        try:
            fn()
        except Exception:
            log.debug("通知 Skill 改动出错", exc_info=True)


# 共享电脑上的访客（#154）：它装的 Skill 放在自己那一份里（skills/guests/<账号>/），只有它看得到、只挂到它的 Agent 上，
# 不同步到别的电脑。处理访客请求、访客的 Skill 助手时设上 SPACE，下面这些函数就都在它那一份里读写；主人的那份它只能看、能用。
SPACE = contextvars.ContextVar("skill_space", default=None)


def root(space=False):
    """space=False：按当前的 SPACE；None：主人的那份。"""
    acct = SPACE.get() if space is False else space
    base = os.path.join(registry.HOME, "skills")
    return os.path.join(base, "guests", acct) if acct else base


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
    """SKILL.md 开头 --- 之间的 name、description（也认 YAML 的多行写法 >- / |）。"""
    m = re.match(r"\s*---\s*\n(.*?)\n---", text or "", re.S)
    out, lines = {}, (m.group(1).splitlines() if m else [])
    for i, line in enumerate(lines):
        if line[:1].isspace():
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k not in ("name", "description"):
            continue
        if v in ("", ">", ">-", ">+", "|", "|-", "|+"):      # 下面缩进的几行是它的内容
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and not nxt[:1].isspace():
                    break
                body.append(nxt.strip())
            v = ("\n" if v.startswith("|") else " ").join(x for x in body if x)
        if v:
            out[k] = v.strip("'\"")
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
                if re.fullmatch(r"[>|][-+]?", (m.get("desc") or "").strip()):   # 以前没认出多行写法的说明：现读
                    with open(os.path.join(_lib(name), "SKILL.md"), encoding="utf-8", errors="replace") as f:
                        m["desc"] = _front(f.read(8000)).get("description", "")
                out.append(m)
    return out


def _files(base):
    out = []
    for dp, dirs, fs in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        out += [os.path.relpath(os.path.join(dp, f), base) for f in sorted(fs) if f not in SKIP]
    return out[:2000]


def detail(name):
    with reading(name) as shared:
        m = _read_meta(_check(name))
        if not m:
            raise Problem("找不到这个 Skill。")
        return {**m, "files": _files(_lib(name)), "repo": repo_of(m.get("source")), **({"shared": True} if shared else {})}


@contextlib.contextmanager
def reading(name):
    """访客看某个 Skill：它自己没有这个名字的，就看主人那份（停用的不给看），给出 True 表示是主人的、只能看。"""
    acct = SPACE.get()
    if not acct or _read_meta(name or "") or not NAME.fullmatch(name or ""):
        yield False
        return
    tok = SPACE.set(None)
    try:
        if (_read_meta(name) or {}).get("disabled"):
            raise Problem("找不到这个 Skill。")
        yield True
    finally:
        SPACE.reset(tok)


def view_listing():
    """Skill 页的列表：访客看到自己装的，加上主人那份里没停用的（标 shared，同名的以它自己的为准）。"""
    acct = SPACE.get()
    if not acct:
        return listing()
    mine = [dict(m, mine=True) for m in listing()]
    tok = SPACE.set(None)
    try:
        theirs = [dict(m, shared=True) for m in listing() if not m.get("disabled")]
    finally:
        SPACE.reset(tok)
    have = {m["name"] for m in mine}
    return sorted(mine + [m for m in theirs if m["name"] not in have], key=lambda m: m["name"].lower())


IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
          ".svg": "image/svg+xml"}


def _inside(base, rel):
    base = os.path.realpath(base)
    p = os.path.realpath(os.path.join(base, rel or ""))
    if not p.startswith(base + os.sep) or not os.path.isfile(p):
        raise Problem("找不到这个文件。")
    return p


def _read_at(base, rel):
    """Skill 里的一个文件：文字给原文，图片给 data 地址，其他的只给大小。"""
    import base64 as b64
    p = _inside(base, rel)
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


def _save_at(base, rel, text, mtime):
    """打开以后被改过就不写。"""
    if not isinstance(text, str):
        raise Problem("内容不对。")
    p = _inside(base, rel)
    if mtime and abs(os.path.getmtime(p) - float(mtime)) > 0.001:
        raise Problem("这个文件在你打开以后被改过了，刷新后再改。")
    with open(p, "w", encoding="utf-8") as f:     # 原地写：软链接指过去的原件、文件权限都保留
        f.write(text)
    return _read_at(base, rel)


def read_file(name, rel):
    with reading(name):
        return _read_at(_lib(_check(name)), rel)


def save_file(name, rel, text, mtime):
    """在网页里改了平台 Skill 的文件：记成这个 Skill 改过，同步到别的电脑。"""
    with LOCK:
        out = _save_at(_lib(_check(name)), rel, text, mtime)
        m = _read_meta(name) or {}
        if rel == "SKILL.md":
            m["desc"] = _front(text).get("description", m.get("desc", ""))
        m.update(updated=time.time(), size=_size(_lib(name)))
        _write(_meta_path(name), json.dumps(m, ensure_ascii=False))
    return out


# ----- Agent 的本地 Skill（#115）：它在自己那台电脑上能用到的，不是平台装的 -----

def _plugin_skill_dirs():
    """装了、开着的插件里带的 Skill：[(插件名, skills 目录)]。"""
    try:
        with open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"), encoding="utf-8") as f:
            inst = json.load(f).get("plugins") or {}
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
            on = json.load(f).get("enabledPlugins") or {}
    except (OSError, ValueError):
        return []
    out = []
    for key, rows in inst.items():
        if not on.get(key):
            continue
        for r in rows or []:
            d = os.path.join(r.get("installPath") or "", "skills")
            if r.get("scope") == "user" and os.path.isdir(d):
                out.append((key.split("@")[0], d))
                break
    return out


def _groups(agent):
    """[(分组, 名字, 目录, 能不能改)]"""
    from . import agents as agents_mod
    home = os.path.realpath(os.path.expanduser("~"))
    if (agent.get("base") or "claude") == "codex":
        return [("user", "这台电脑通用的", os.path.expanduser("~/.codex/skills"), True)]
    cwd = os.path.realpath(agents_mod.cwd(agent))
    out = [] if cwd == home else [("project", "工作目录里的", os.path.join(cwd, ".claude", "skills"), True)]
    out.append(("user", "这台电脑通用的", os.path.expanduser("~/.claude/skills"), True))
    out += [(f"plugin:{n}", f"插件「{n}」带的", d, False) for n, d in _plugin_skill_dirs()]
    return out


def agent_skills(agent):
    have = {m["name"] for m in listing()}
    out = []
    for gid, label, d, editable in _groups(agent):
        rows = []
        for n in sorted(os.listdir(d) if os.path.isdir(d) else [], key=str.lower):
            p = os.path.join(d, n)
            if n.startswith(".") or not os.path.isfile(os.path.join(p, "SKILL.md")):
                continue
            with open(os.path.join(p, "SKILL.md"), encoding="utf-8", errors="replace") as f:
                fm = _front(f.read(6000))
            rows.append({"name": n, "desc": fm.get("description", ""), "link": os.path.realpath(p) if os.path.islink(p) else None,
                         "shared": n in have})
        if rows:
            out.append({"id": gid, "label": label, "editable": editable, "skills": rows})
    return out


def _agent_dir(agent, group, name, label=False):
    for gid, lab, d, editable in _groups(agent):
        if gid == group:
            p = os.path.join(d, name or "")
            if not name or "/" in name or name.startswith(".") or not os.path.isfile(os.path.join(p, "SKILL.md")):
                break
            return (os.path.realpath(p), editable, os.path.islink(p)) + ((lab,) if label else ())
    raise Problem("找不到这个 Skill。")


def agent_detail(agent, group, name):
    d, editable, link, label = _agent_dir(agent, group, name, label=True)
    with open(os.path.join(d, "SKILL.md"), encoding="utf-8", errors="replace") as f:
        fm = _front(f.read(6000))
    return {"name": name, "group": group, "label": label, "desc": fm.get("description", ""), "editable": editable,
            "link": d if link else None, "files": _files(d), "size": _size(d), "shared": bool(_read_meta(name))}


def agent_read(agent, group, name, rel):
    return _read_at(_agent_dir(agent, group, name)[0], rel)


def agent_save(agent, group, name, rel, text, mtime):
    d, editable, _ = _agent_dir(agent, group, name)
    if not editable:
        raise Problem("插件带的 Skill 只能看，改了插件更新时会被覆盖。")
    return _save_at(d, rel, text, mtime)


def agent_share(agent, group, name, machine_name=""):
    """把 Agent 的本地 Skill 提交成平台 Skill（同名的换成这一份）。"""
    d, _, _ = _agent_dir(agent, group, name)
    if not NAME.fullmatch(name):
        raise Problem(f"「{name}」不能当 Skill 的名字，只能用英文字母、数字、点、横线、下划线。")
    return [_put(d, {"type": "local", "machine": machine_name, "agent": agent.get("name") or ""}, name=name)]


# ----- 各个 Skill 市场里有没有（#115） -----

MARKETS = [("skills.sh", "https://skills.sh", "https://skills.sh/?q={q}"),
           ("ClawHub", "https://clawhub.ai", "https://clawhub.ai/skills?q={q}"),
           ("SkillsMP", "https://skillsmp.com", "https://skillsmp.com/search?q={q}")]
_market_cache = {}


def _get_json(url):
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "ccim", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
        return json.loads(r.read(4 << 20))


def _one_market(market, name, repo):
    from urllib.parse import quote
    q, low = quote(name), name.lower()
    try:
        if market == "skills.sh":
            rows = [r for r in _get_json(f"https://skills.sh/api/search?q={q}").get("skills") or [] if (r.get("skillId") or r.get("name") or "").lower() == low]
            same = [r for r in rows if repo and (r.get("source") or "").lower() == repo]
            best = max(same or rows, key=lambda r: r.get("installs") or 0, default=None)
            return best and {"url": "https://skills.sh/" + best["id"], "stat": "安装", "n": best.get("installs") or 0,
                             "where": best.get("source"), "same": bool(same)}
        if market == "ClawHub":
            rows = [r for r in _get_json(f"https://clawhub.ai/api/v1/search?q={q}").get("results") or []
                    if (r.get("canonicalUrl") or "").rstrip("/").split("/")[-1].lower() == low or (r.get("displayName") or "").lower() == low]
            best = max(rows, key=lambda r: r.get("downloads") or 0, default=None)
            return best and {"url": "https://clawhub.ai" + best["canonicalUrl"], "stat": "下载", "n": best.get("downloads") or 0,
                             "where": ((best.get("native") or {}).get("ownerHandle")), "same": False}
        if market == "SkillsMP":
            rows = [r for r in _get_json(f"https://skillsmp.com/api/skills?search={q}").get("skills") or [] if (r.get("name") or "").lower() == low]
            same = [r for r in rows if repo and f"github.com/{repo}/" in (r.get("githubUrl") or "").lower() + "/"]
            best = max(same or rows, key=lambda r: r.get("stars") or 0, default=None)
            return best and {"url": "https://skillsmp.com/skills/" + best["id"], "stat": "仓库星数", "n": best.get("stars") or 0,
                             "where": f"{best.get('author')}", "same": bool(same)}
    except Exception as e:
        log.info("查 %s 出错：%s", market, e)
        return {"error": True}
    return None


def market(name, repo=None):
    """这个 Skill 在各个市场里的收录情况：同一个仓库的优先（same），没有就按名字。结果缓存 6 小时。"""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote
    if not name:
        raise Problem("找不到这个 Skill。")
    repo = (repo or "").lower() or None
    key = (name.lower(), repo)
    hit = _market_cache.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    with ThreadPoolExecutor(3) as ex:
        found = list(ex.map(lambda m: _one_market(m[0], name, repo), MARKETS))
    out = {"markets": [{"name": m, "home": home, "search": search.format(q=quote(name)), **({"hit": f} if f and not f.get("error") else {}),
                        **({"error": True} if f and f.get("error") else {})} for (m, home, search), f in zip(MARKETS, found)]}
    if not any(f and f.get("error") for f in found):
        _market_cache[key] = (time.time(), out)
    return out


def repo_of(source):
    """从 GitHub 装的：owner/repo。"""
    try:
        if (source or {}).get("type") == "github":
            o, r, _, _ = _github(source["url"])
            return f"{o}/{r}"
    except Problem:
        pass
    return None


def write_any(name, rel, text):
    """Skill 助手（#116）写文件：没有的 Skill、没有的文件都新建；写 SKILL.md 时校验开头的 name、description。"""
    _check(name)
    rel = (rel or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/") or rel.split("/")[0] in SKIP:
        raise Problem("路径不对。")
    if not isinstance(text, str):
        raise Problem("内容不对。")
    if rel == "SKILL.md" and not _front(text).get("description"):
        raise Problem("SKILL.md 开头要有 --- 包起来的 name 和 description。")
    with LOCK:
        d = _lib(name)
        if not os.path.isfile(os.path.join(d, "SKILL.md")) and rel != "SKILL.md":
            raise Problem(f"还没有「{name}」这个 Skill，先写它的 SKILL.md。")
        p = os.path.realpath(os.path.join(d, rel))
        if not p.startswith(os.path.realpath(d) + os.sep):
            raise Problem("路径不对。")
        _write(p, text)
        m = _read_meta(name) or {"name": name, "source": {"type": "assistant"}, "installed": time.time(), "disabled": False}
        if rel == "SKILL.md":
            m["desc"] = _front(text).get("description", "")
        m.update(updated=time.time(), size=_size(d))
        _write(_meta_path(name), json.dumps(m, ensure_ascii=False))
        t = _tombs()
        if name in t:
            t.pop(name)
            _write(os.path.join(root(), "_tombstones.json"), json.dumps(t))
    changed()
    return {"name": name, "path": rel}


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


def import_local(name, machine_name="", machine_id=""):
    """从这台电脑的 ~/.claude/skills 拷进平台（已有就覆盖）。记下电脑编号，Skill 页「更新」时转到这台电脑重新导入（#275）。"""
    d = os.path.realpath(os.path.join(os.path.expanduser("~/.claude/skills"), _check(name)))
    if not os.path.isfile(os.path.join(d, "SKILL.md")):
        raise Problem(f"这台电脑的 ~/.claude/skills 里没有「{name}」。")
    return [_put(d, {"type": "local", "machine": machine_name, **({"mid": machine_id} if machine_id else {})}, name=name)]


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
    tok = SPACE.set(None)
    try:
        dirs = {m["name"]: _lib(m["name"]) for m in listing() if not m.get("disabled") and m["name"] not in off}
        acct = (agent or {}).get("account")
        if acct:                              # 共享电脑上访客的 Agent（#154）：再加上它自己装的，同名的用它自己的
            SPACE.set(acct)
            dirs.update({m["name"]: _lib(m["name"]) for m in listing() if not m.get("disabled") and m["name"] not in off})
    finally:
        SPACE.reset(tok)
    names = sorted(dirs)
    if not names:
        return None
    key = re.sub(r"[^\w-]", "_", (agent or {}).get("id") or "all")
    view = os.path.join(root(None), "views", key)
    with LOCK:
        try:                                  # 没变就不重建：同一个 Agent 的别的会话可能正在用它
            sk = os.path.join(view, "skills")
            if sorted(os.listdir(sk)) == names and all(os.readlink(os.path.join(sk, n)) == dirs[n] for n in names):
                return view
        except OSError:
            pass
        shutil.rmtree(view, ignore_errors=True)
        os.makedirs(os.path.join(view, ".claude-plugin"))
        os.makedirs(os.path.join(view, "skills"))
        with open(os.path.join(view, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as f:
            json.dump({"name": PLUGIN, "description": "ccim 平台上装的 Skill", "version": "1.0.0"}, f, ensure_ascii=False)
        for n in names:
            os.symlink(dirs[n], os.path.join(view, "skills", n))
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
