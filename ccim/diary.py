"""日记（#363）：在任何对话里发 /diary 内容，记到本机的 Markdown 日记里，不交给助手。

按天一个文件（YYYY-MM-DD.md，和 Obsidian 日记插件默认的文件名一样），放在「文稿 / 文档」下的 ccim-日记，
整个文件夹可以直接当 Obsidian 库打开。每条是「## 时间 · 记在哪」加正文，用户在 Obsidian 里改了也照样能读、能搜。
网页里记的，「记在哪」写成 Markdown 链接，指向记它的那个对话里的那张日记卡片（完整网址，Obsidian 里点了用浏览器打开）；
以前没带链接的照常读。
助手用 diary_read / diary_search 两个工具查（session.py 的 _diary_tools）。

几台电脑时日记集中存在一台上（#376，peers.diary_home）：平台进程按它转过去写、去查（Platform.diary_write / diary_query），
那台不在线时先存进本机的 diary-pending.jsonl，连上后按原来的时间补写；飞书、微信的进程交给本机平台，平台没在运行也先存着。
设置页能在那台电脑上用访达、VS Code、Obsidian 打开日记文件夹（apps / open_in）。
"""
import datetime, json, os, platform as _platform, re, secrets, shutil, subprocess, threading

from . import registry

CMD = re.compile(r"^/(diary|日记)(?=\s|$)\s*", re.I)
HEAD = re.compile(r"^## (\d{1,2}:\d{2})(?: · (.*))?$")
LINKED = re.compile(r"^\[(.*)\]\((https?://\S+)\)$")     # 「记在哪」带链接的写法
READ_DAYS = 62            # diary_read 一次最多读几天
READ_MAX = 40000          # 一次最多给助手多少字
SEARCH_MAX = 40           # 搜索最多给几条
ATTACH = "附件"           # 日记带的图片、文件（#394）放在日记文件夹下的这里
FILES_MAX = 50 * 1024 * 1024   # 一条日记的附件最多这么大（转给存日记的电脑时要整个带过去）
IMG = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg|heic)$", re.I)


def root():
    # Windows 的「文档」也在用户目录下的 Documents
    return os.path.join(os.path.expanduser("~"), "Documents", "ccim-日记")


def parse(text):
    """「/diary 内容」→ 内容；不是 /diary 返回 None（内容为空返回 ""）。"""
    m = CMD.match((text or "").lstrip())
    return (text or "").lstrip()[m.end():].strip() if m else None


_lock = threading.Lock()


def _minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _save_files(files, now):
    """附件（#394）存进「附件」，返回正文里引用它们的几行：图片嵌入，别的是链接。files：[{name, data}]，data 是 bytes。"""
    d = os.path.join(root(), ATTACH)
    os.makedirs(d, exist_ok=True)
    lines = []
    for f in files:
        name = re.sub(r"[\x00-\x1f/\\:<>\[\]]", "_", f.get("name") or "").strip() or "file"
        fn = f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{name}"
        with open(os.path.join(d, fn), "wb") as fh:
            fh.write(f["data"])
        lines.append(f"{'!' if IMG.search(name) else ''}[{name}](<{ATTACH}/{fn}>)")
    return lines


def add(text, where, now=None, link=None, files=None):
    """记一条，返回 {date, time, where, path}。link：回到记它的地方的完整网址（网页里记的才有）。
    now：记的时间（别的电脑记的、暂存后补写的用原来的时间，#376）：写进那天的文件，比已有的早就插到它们前面。
    files：带的图片、文件（#394），[{name, data}]。"""
    text = (text or "").strip()
    if not text and not files:
        raise ValueError("在 /diary 后面写上要记的内容。")
    now = now or datetime.datetime.now()
    d, t = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    f = os.path.join(root(), d + ".md")
    where = " ".join((where or "").split())
    label = f" · [{where.replace('[', '(').replace(']', ')') or '原对话'}]({link})" if link else f" · {where}" if where else ""
    with _lock:
        os.makedirs(root(), exist_ok=True)
        body = "\n\n".join(x for x in [text] + (_save_files(files, now) if files else []) if x)
        block = f"## {t}{label}\n\n{body}\n"
        try:
            with open(f, encoding="utf-8") as fh:
                old = fh.read()
        except FileNotFoundError:
            old = ""
        pos, off = None, 0
        for ln in old.splitlines(keepends=True):     # 第一条比它晚的标题
            m = HEAD.match(ln.rstrip("\r\n"))
            if m and _minutes(m.group(1)) > _minutes(t):
                pos = off
                break
            off += len(ln)
        if pos is None:                              # 最常见：追加在最后
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(("" if not old.strip() else "\n" if old.endswith("\n") else "\n\n") + block)
        else:
            tmp = f + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(old[:pos] + block + "\n" + old[pos:])
            os.replace(tmp, f)
    return {"date": d, "time": t, "where": where, "path": f}


def when(at):
    """传来的时间（ISO 写法）→ datetime；没有或不对就是现在。"""
    try:
        return datetime.datetime.fromisoformat(str(at)) if at else datetime.datetime.now()
    except ValueError:
        return datetime.datetime.now()


# ---------- 存在哪台电脑（#376） ----------

def _pending_file():
    return os.path.join(registry.HOME, "diary-pending.jsonl")


def _pending_dir():
    return os.path.join(registry.HOME, "diary-pending")


def hold(item, files=None):
    """存日记的电脑不在线：先存在本机，平台连上它以后补写（take_pending）。几个进程都可能写，一行一条追加。
    files（[{name, data}]）存进 diary-pending 文件夹，item 里记 [{name, path}]；已经是 path 的照旧。"""
    os.makedirs(registry.HOME, exist_ok=True)
    if files:
        os.makedirs(_pending_dir(), exist_ok=True)
        kept = []
        for f in files:
            if "data" not in f:
                kept.append(f)
                continue
            p = os.path.join(_pending_dir(), secrets.token_hex(6))
            with open(p, "wb") as fh:
                fh.write(f["data"])
            kept.append({"name": f.get("name") or "file", "path": p})
        item = dict(item, files=kept)
    with open(_pending_file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def pending_files(item):
    """暂存的那条带的附件读出来：[{name, data}]。只认 diary-pending 文件夹里的。"""
    d = os.path.realpath(_pending_dir()) + os.sep
    out = []
    for f in item.get("files") or []:
        p = os.path.realpath(str(f.get("path") or ""))
        if p.startswith(d) and os.path.isfile(p):
            with open(p, "rb") as fh:
                out.append({"name": f.get("name") or "file", "data": fh.read()})
    return out


def drop_pending_files(item):
    d = os.path.realpath(_pending_dir()) + os.sep
    for f in item.get("files") or []:
        p = os.path.realpath(str(f.get("path") or ""))
        if p.startswith(d):
            try:
                os.remove(p)
            except OSError:
                pass


def take_pending():
    """取出暂存的（取走就从文件里拿掉）；没写成的再 hold 回去。"""
    f = _pending_file()
    if not os.path.exists(f):
        return []
    work = f + f".{os.getpid()}"
    try:
        os.replace(f, work)
    except OSError:
        return []
    out = []
    with open(work, encoding="utf-8") as fh:
        for ln in fh:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    os.remove(work)
    return out


def _platform_api(path, body):
    """交给本机的平台（飞书、微信的进程用）。平台没在运行抛 OSError；平台说不行抛 ValueError。"""
    import urllib.error, urllib.request
    from .platform import url
    req = urllib.request.Request(url().rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"X-CCIM": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except ValueError:
            msg = None
        raise ValueError(msg or "日记出错了，再试一次。")
    except urllib.error.URLError as e:
        raise OSError(str(e))


def _here():
    """存日记的就是这台电脑（没加入别的电脑、或设的就是这台）。"""
    from . import peers
    return peers.diary_home_file() == peers.identity()["id"]


def record(text, where):
    """飞书、微信里记的（#376）：存日记的是这台就直接写；否则交给本机平台转过去，平台没在运行先存着。
    返回 {date, time}，暂存的多一个 pending。"""
    now = datetime.datetime.now()
    if _here():
        return add(text, where, now)
    if not (text or "").strip():
        raise ValueError("在 /diary 后面写上要记的内容。")
    item = {"text": text, "where": where, "at": now.isoformat(timespec="seconds")}
    try:
        return _platform_api("/api/diary/add", item)
    except OSError:
        hold(item)
        return {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "pending": True}


def query(op, **kw):
    """Agent 查日记（diary_read / diary_search）：存在这台就直接查，否则经本机平台去那台查。"""
    if _here():
        return read(kw.get("start"), kw.get("end")) if op == "read" else search(kw.get("query") or "")
    try:
        return _platform_api("/api/diary/query", dict(kw, op=op))["text"]
    except OSError:
        raise ValueError("日记存在另一台电脑上，这台电脑上的 ccim 网页平台没在运行，查不到。")


def run_query(op, kw):
    return read(kw.get("start"), kw.get("end")) if op == "read" else search(kw.get("query") or "")


# ---------- 在那台电脑上打开日记文件夹（#376） ----------

def _system():
    return _platform.system()


def _mac_app(name, bundle):
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        if os.path.isdir(os.path.join(d, name + ".app")):
            return True
    try:
        r = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle}'"], capture_output=True, text=True, timeout=3)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _win_exe(*rel):
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        for r in rel:
            p = os.path.join(base or "", r)
            if base and os.path.isfile(p):
                return p
    return None


def _win_vscode():
    return _win_exe(os.path.join("Programs", "Microsoft VS Code", "Code.exe"), os.path.join("Microsoft VS Code", "Code.exe"))


def _win_obsidian():
    if _win_exe(os.path.join("Programs", "Obsidian", "Obsidian.exe"), os.path.join("Obsidian", "Obsidian.exe")):
        return True
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "obsidian"))
        return True
    except Exception:
        return False


def apps():
    """这台电脑上能打开日记文件夹的：[{id, name}]，没装的不列。"""
    sysname = _system()
    out = [{"id": "files", "name": {"Darwin": "访达", "Windows": "资源管理器"}.get(sysname, "文件管理器")}]
    if sysname == "Darwin":
        code, obs = _mac_app("Visual Studio Code", "com.microsoft.VSCode"), _mac_app("Obsidian", "md.obsidian")
    elif sysname == "Windows":
        code, obs = bool(_win_vscode() or shutil.which("code")), _win_obsidian()
    else:
        code, obs = bool(shutil.which("code")), bool(shutil.which("obsidian"))
    if code:
        out.append({"id": "vscode", "name": "VS Code"})
    if obs:
        out.append({"id": "obsidian", "name": "Obsidian"})
    return out


def _open_url(u):
    sysname = _system()
    if sysname == "Darwin":
        subprocess.run(["open", u], capture_output=True, timeout=15)
    elif sysname == "Windows":
        os.startfile(u)
    else:
        subprocess.Popen(["xdg-open", u], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def obsidian_config():
    """Obsidian 记库列表的文件。"""
    sysname = _system()
    if sysname == "Darwin":
        return os.path.expanduser("~/Library/Application Support/obsidian/obsidian.json")
    if sysname == "Windows":
        return os.path.join(os.environ.get("APPDATA") or "", "obsidian", "obsidian.json")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "obsidian", "obsidian.json")


def obsidian_running():
    """Obsidian 开着吗：它开着时会改写库列表，这时不去动。看不出来就当开着。"""
    sysname = _system()
    try:
        if sysname == "Windows":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"], capture_output=True, text=True, timeout=5)
            return "obsidian.exe" in r.stdout.lower()
        r = subprocess.run(["pgrep", "-x", "Obsidian" if sysname == "Darwin" else "obsidian"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return True


def obsidian_vault(folder, cfg=None, running=None):
    """日记文件夹在 Obsidian 里的库编号：已经是库就用它；不是的，Obsidian 没开着时加进库列表（先备份原文件）。
    加不了返回 None。"""
    cfg = cfg or obsidian_config()
    real = os.path.realpath(folder)
    try:
        with open(cfg, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = None
    except (OSError, ValueError):
        return None                              # 读不懂的不碰
    if data is not None and not isinstance(data, dict):
        return None
    vaults = (data or {}).get("vaults")
    if vaults is not None and not isinstance(vaults, dict):
        return None
    for vid, v in (vaults or {}).items():
        if isinstance(v, dict) and isinstance(v.get("path"), str) and os.path.realpath(v["path"]) == real:
            return vid
    if not os.path.isdir(os.path.dirname(cfg)):   # 没打开过 Obsidian：不替它建配置
        return None
    if running if running is not None else obsidian_running():
        return None
    vid = secrets.token_hex(8)
    data = dict(data or {})
    data["vaults"] = dict(vaults or {}, **{vid: {"path": folder, "ts": int(datetime.datetime.now().timestamp() * 1000)}})
    if os.path.exists(cfg):
        shutil.copy2(cfg, cfg + ".ccim-bak")
    tmp = cfg + ".ccim-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, cfg)
    return vid


def open_in(app):
    """在这台电脑上用 app 打开日记文件夹。返回 {}；Obsidian 没能直接打开这个库时返回 {"pick": True}（打开了选库的界面）。"""
    if app not in {a["id"] for a in apps()}:
        raise ValueError("这台电脑上没有这个应用。")
    d = root()
    os.makedirs(d, exist_ok=True)
    sysname = _system()
    if app == "files":
        if sysname == "Darwin":
            subprocess.run(["open", d], capture_output=True, timeout=15)
        elif sysname == "Windows":
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {}
    if app == "vscode":
        if sysname == "Darwin":
            r = subprocess.run(["open", "-b", "com.microsoft.VSCode", d], capture_output=True, timeout=15)
            if r.returncode:
                subprocess.run(["open", "-a", "Visual Studio Code", d], capture_output=True, timeout=15)
        elif sysname == "Windows":
            exe = _win_vscode()
            subprocess.Popen([exe, d] if exe else ["cmd", "/c", "code", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["code", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {}
    from urllib.parse import quote
    vid = obsidian_vault(d)
    if vid:
        _open_url("obsidian://open?vault=" + quote(vid))
        return {}
    _open_url("obsidian://choose-vault")
    return {"pick": True}


def _entries(d):
    """某一天的日记：[{time, where, link, text}]。开头不在任何「## 时间」下面的内容算一条没有时间的。"""
    try:
        with open(os.path.join(root(), d + ".md"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out, cur = [], {"time": "", "where": "", "link": "", "lines": []}
    for ln in lines:
        m = HEAD.match(ln)
        if m:
            out.append(cur)
            where, link = (m.group(2) or "").strip(), ""
            lk = LINKED.match(where)
            if lk:
                where, link = lk.group(1), lk.group(2)
            cur = {"time": m.group(1), "where": where, "link": link, "lines": []}
        else:
            cur["lines"].append(ln)
    out.append(cur)
    return [{"time": e["time"], "where": e["where"], "link": e["link"], "text": "\n".join(e["lines"]).strip()}
            for e in out if "\n".join(e["lines"]).strip()]


def _days():
    try:
        return sorted(f[:-3] for f in os.listdir(root()) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f))
    except OSError:
        return []


def _date(s, default):
    s = (s or "").strip()
    if not s:
        return default
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"日期 {s} 不对，要写成 2026-01-31 这样。")


def site_link(link):
    """完整网址 → 站内链接（#/ 开头那段），网页里点了在当前页打开；不是平台的网址返回空。"""
    i = (link or "").find("#/")
    return link[i:] if i >= 0 else ""


def _fmt(d, e):
    e = dict(e, text=e["text"].replace(f"](<{ATTACH}/", f"](<{os.path.join(root(), ATTACH)}/"))   # 附件写成完整路径，助手能直接打开
    site = site_link(e.get("link"))
    where = f"[{e['where'] or '原对话'}]({site})" if site else e["where"]
    head = " · ".join(x for x in (d + (" " + e["time"] if e["time"] else ""), where) if x)
    return f"### {head}\n{e['text']}"


def read(start=None, end=None, today=None):
    """start ~ end（含两头）的日记，整理成文字给助手。都不写就是今天；只写 start 就是那一天。"""
    today = today or datetime.date.today()
    a = _date(start, today)
    b = _date(end, a if start else today)
    if b < a:
        a, b = b, a
    if (b - a).days >= READ_DAYS:
        a = b - datetime.timedelta(days=READ_DAYS - 1)
        cut = f"（一次最多读 {READ_DAYS} 天，只给了 {a} 到 {b}）\n\n"
    else:
        cut = ""
    parts = []
    for d in _days():
        if a.isoformat() <= d <= b.isoformat():
            parts += [_fmt(d, e) for e in _entries(d)]
    if not parts:
        return f"{a} 到 {b} 没有日记。" if a != b else f"{a} 没有日记。"
    text = cut + "\n\n".join(parts)
    return text if len(text) <= READ_MAX else text[:READ_MAX] + "\n\n（太长了，后面的没给，缩小日期范围再读）"


def search(query, limit=SEARCH_MAX):
    """按关键词找日记：几个词（空格分开）都出现在同一条里才算，新的在前。"""
    words = [w.lower() for w in (query or "").split()]
    if not words:
        raise ValueError("写上要找的关键词。")
    hits = []
    for d in reversed(_days()):
        for e in reversed(_entries(d)):
            if all(w in (e["text"] + " " + e["where"]).lower() for w in words):
                hits.append(_fmt(d, e))
                if len(hits) >= limit:
                    return "\n\n".join(hits) + f"\n\n（只列了最近的 {limit} 条，换个更具体的词再找）"
    return "\n\n".join(hits) if hits else "日记里没找到。"
