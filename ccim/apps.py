"""应用（#103）：一个应用 = 一个页面 + 一个数据目录 + 给 Agent 的说明，放在 ~/.ccim/apps/<编号>/。

Agent 照 README（GUIDE）直接在这台电脑上建、改应用、往 data/ 里写数据；网页把页面放进隔离的 iframe，
页面只能通过 window.ccim（网页转成下面这几个接口）读写自己的 data/。整个目录是一个 git 仓库，每次改动一个提交，可以退回。
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid

from . import registry

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PAGE_MAX = 2 * 1024 * 1024        # index.html 最大多少（页面整个放进 srcdoc）
DATA_MAX = 5 * 1024 * 1024        # 一次写入最多多少
GUIDE = """\
# ccim 应用

用户在 ccim 网页左侧「应用」里打开这里的应用，也能在对话右侧打开。一个应用一个文件夹，文件夹名就是编号（小写字母、数字、横线）：

```
<编号>/
  app.json      名字、说明、图标、数据接口（见下）
  index.html    页面，所有样式和脚本写在这一个文件里
  AGENT.md      给 Agent 的补充说明：每类数据怎么填、注意什么
  data/         数据，页面和 Agent 共用
```

## app.json 和数据接口

```json
{
  "name": "竞品记录", "desc": "一句话说明", "icon": "🔭", "agent": "做这个应用的 Agent 编号（可空）",
  "data": {
    "items": {
      "name": "竞品", "desc": "一个竞品一条",
      "fields": {
        "name":    {"type": "string", "required": true, "desc": "产品名"},
        "status":  {"type": "string", "enum": ["待体验", "体验过"], "desc": "体验状态"},
        "tags":    {"type": "list", "desc": "标签"},
        "updated": {"type": "date", "desc": "平台自动填"}
      }
    }
  }
}
```

- `data` 下每一项是一类数据（集合），每条记录存成 `data/<集合>/<编号>.json`
- 字段类型：string、number、boolean、list、object、date（date 字段平台自动填当天；名叫 added 的只在新建时填）
- Agent 用 ccim 的 app_list、app_query、app_save、app_delete 工具按这个接口增删改查，不要直接写 data/ 里的文件；
  用 app_open 在用户的对话右侧打开应用
- 名字以 app.json 里的为准，用户在网页上可以改名

## 页面

页面跑在隔离的框里，不能直接访问平台和本机文件，只能用平台给的 `window.ccim`（都返回 Promise）：

| 调用 | 作用 |
|---|---|
| `ccim.query(集合, 关键词?)` | 这类数据的所有记录（每条带 id），按更新时间新的在前 |
| `ccim.get(集合, id)` | 读一条，没有返回 null |
| `ccim.save(集合, 记录)` | 新建或更新（带 id 就是更新，只改给了的字段），返回存好的记录 |
| `ccim.del(集合, id)` | 删一条 |
| `ccim.read(path)` / `ccim.write(path, text)` / `ccim.list(dir?)` / `ccim.remove(path)` | 不走接口、直接读写 data/ 下的文件 |
| `ccim.copy(text, html?)` | 复制到剪贴板；给了 html 就带格式复制（贴到公众号编辑器等） |
| `ccim.chat(text)` | 打开一个和做这个应用的 Agent 的新对话，输入框里先填好 text |
| `ccim.dev()` | ccim 自己的需求（只读，只有开发版的电脑有，别的返回 null）：`{review, shipped, trying}`；review 是待上线的（pr、issue、title、url、checks、trying、updated），shipped 是已上线的（issue、pr、title、url、merged、rounds） |

数据变了（Agent 写了新东西）会通知页面：`ccim.onChange(fn)`；页面文件改了，打开着的会自动重新载入。
页面要配合平台的浅色界面：白底、深灰字、圆角、留白；手机上也要能用（宽度 360 起）。不要用浏览器原生的下拉框、alert、confirm。
平台会把这个目录的改动自动提交到 git，做坏了可以用 git 退回。做好、改好之后用 app_open 打开给用户看。
"""


def root():
    return os.path.join(registry.HOME, "apps")


def ensure():
    d = root()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "README.md")
    try:
        old = open(p, encoding="utf-8").read()
    except OSError:
        old = None
    if old != GUIDE:
        with open(p, "w", encoding="utf-8") as f:
            f.write(GUIDE)
    if not os.path.isdir(os.path.join(d, ".git")) and shutil.which("git"):
        _git("init", "-q")
        with open(os.path.join(d, ".gitignore"), "w") as f:
            f.write(".DS_Store\n")
    return d


def _git(*args):
    return subprocess.run(["git", "-C", root(), "-c", "user.name=ccim", "-c", "user.email=ccim@localhost", *args],
                          capture_output=True, text=True, timeout=20)


def commit(msg="更新应用"):
    """有改动就提交一次（Agent 直接改的文件在下一次列出、读写时补上提交）。"""
    if not shutil.which("git") or not os.path.isdir(os.path.join(root(), ".git")):
        return
    try:
        if _git("status", "--porcelain").stdout.strip():
            _git("add", "-A")
            _git("commit", "-q", "-m", msg)
    except (OSError, subprocess.SubprocessError):
        pass


class Problem(ValueError):
    pass


def _dir(app):
    if not isinstance(app, str) or not ID.match(app):
        raise Problem("没有这个应用。")
    d = os.path.join(root(), app)
    if not os.path.isfile(os.path.join(d, "app.json")):
        raise Problem("没有这个应用。")
    return d


def _meta(d):
    try:
        m = json.load(open(os.path.join(d, "app.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _updated(d):
    t = 0
    for base, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            try:
                t = max(t, os.path.getmtime(os.path.join(base, f)))
            except OSError:
                pass
    return t


def listing():
    ensure()
    commit()
    out = []
    for name in sorted(os.listdir(root())):
        d = os.path.join(root(), name)
        m = ID.match(name) and os.path.isdir(d) and _meta(d)
        if not m:
            continue
        out.append({"id": name, "name": str(m.get("name") or name), "desc": str(m.get("desc") or ""),
                    "icon": str(m.get("icon") or "")[:4], "agent": m.get("agent") or None,
                    "ready": os.path.isfile(os.path.join(d, "index.html")), "updated": _updated(d)})
    out.sort(key=lambda a: -a["updated"])
    return out


def page(app):
    d = _dir(app)
    m = _meta(d) or {}
    p = os.path.join(d, "index.html")
    if not os.path.isfile(p):
        raise Problem("这个应用还没有页面。")
    if os.path.getsize(p) > PAGE_MAX:
        raise Problem("页面太大了，打不开。")
    return {"id": app, "name": str(m.get("name") or app), "icon": str(m.get("icon") or "")[:4],
            "agent": m.get("agent") or None, "html": open(p, encoding="utf-8", errors="replace").read(),
            "sig": data_sig(app), "psig": page_sig(app)}


def _data(app, path, make=False):
    base = os.path.join(_dir(app), "data")
    if make:
        os.makedirs(base, exist_ok=True)
    path = (path or "").strip().lstrip("/")
    full = os.path.realpath(os.path.join(base, path))
    real = os.path.realpath(base)
    if full != real and not full.startswith(real + os.sep):
        raise Problem("路径不对。")
    return full


def data_list(app, sub=""):
    top = _data(app, sub)
    base = os.path.realpath(_data(app, ""))
    out = []
    if os.path.isdir(top):
        for b, dirs, files in os.walk(top):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(files):
                if f.startswith("."):
                    continue
                p = os.path.join(b, f)
                st = os.stat(p)
                out.append({"path": os.path.relpath(p, base), "size": st.st_size, "mtime": st.st_mtime})
    return {"files": out}


def data_read(app, path):
    p = _data(app, path)
    if not os.path.isfile(p):
        return {"text": None}
    if os.path.getsize(p) > DATA_MAX:
        raise Problem("文件太大了。")
    return {"text": open(p, encoding="utf-8", errors="replace").read()}


def data_write(app, path, text):
    if not isinstance(text, str):
        raise Problem("内容不对。")
    if len(text.encode()) > DATA_MAX:
        raise Problem("内容太大了。")
    p = _data(app, path, make=True)
    if os.path.isdir(p):
        raise Problem("路径不对。")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
    commit(f"{app}：改了 {path}")
    return {"sig": data_sig(app)}


def data_remove(app, path):
    p = _data(app, path)
    if os.path.isfile(p):
        os.remove(p)
        commit(f"{app}：删了 {path}")
    return {"sig": data_sig(app)}


def data_sig(app):
    """data/ 的变化标记：网页每隔几秒问一次，变了就通知页面（Agent 写进了新数据）。"""
    return str(round(_updated(os.path.join(_dir(app), "data")), 3))


def page_sig(app):
    """页面的变化标记：index.html、app.json 改了，打开着的页面重新载入（热重载）。"""
    d = _dir(app)
    t = 0
    for f in ("index.html", "app.json"):
        try:
            t = max(t, os.path.getmtime(os.path.join(d, f)))
        except OSError:
            pass
    return str(round(t, 3))


# ---- 数据接口：app.json 的 data 声明集合和字段，Agent（ccim 工具）和页面（window.ccim）都按它增删改查 ----
TYPES = {"string": str, "number": (int, float), "boolean": bool, "list": list, "object": dict, "date": str}
REC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def meta(app):
    return _meta(_dir(app)) or {}


def collections(app):
    c = meta(app).get("data")
    return c if isinstance(c, dict) else {}


def _coll(app, coll):
    c = collections(app)
    if coll not in c or not ID.match(str(coll)):
        names = "、".join(c) or "（这个应用没有声明数据接口）"
        raise Problem(f"「{meta(app).get('name') or app}」没有「{coll}」这类数据，有：{names}")
    spec = c[coll] if isinstance(c[coll], dict) else {}
    return spec, _data(app, coll, make=True)


def _new_id(rec, spec):
    for k, f in (spec.get("fields") or {}).items():
        v = rec.get(k)
        if isinstance(v, str) and (f or {}).get("required"):
            slug = re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")[:40]
            if slug and REC_ID.match(slug):
                return slug
            break
    return uuid.uuid4().hex[:10]


def _load(path):
    try:
        v = json.load(open(path, encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def query(app, coll, text="", rid=None, limit=200):
    spec, d = _coll(app, coll)
    if rid:
        if not REC_ID.match(str(rid)):
            return None
        r = _load(os.path.join(d, f"{rid}.json"))
        return {**r, "id": rid} if r is not None else None
    out = []
    words = [w.lower() for w in (text or "").split() if w]
    for f in os.listdir(d) if os.path.isdir(d) else []:
        if not f.endswith(".json") or f.startswith("."):
            continue
        r = _load(os.path.join(d, f))
        if r is None:
            continue
        blob = json.dumps(r, ensure_ascii=False).lower()
        if all(w in blob for w in words):
            out.append({**r, "id": f[:-5], "_t": os.path.getmtime(os.path.join(d, f))})
    out.sort(key=lambda r: -r.pop("_t"))
    return out[:limit]


def save(app, coll, rec):
    if not isinstance(rec, dict):
        raise Problem("记录要是一个对象。")
    spec, d = _coll(app, coll)
    fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {}
    rid = rec.get("id")
    if rid is not None and not REC_ID.match(str(rid)):
        raise Problem("id 只能用字母、数字、横线、下划线。")
    old = query(app, coll, rid=rid) if rid else None
    new = {**(old or {}), **{k: v for k, v in rec.items() if k != "id"}}
    new.pop("id", None)
    today = time.strftime("%Y-%m-%d")
    probs = []
    for k, f in fields.items():
        f = f if isinstance(f, dict) else {}
        t = f.get("type") or "string"
        if t == "date" and (k != "added" or not old) and k not in rec:
            new[k] = today
        v = new.get(k)
        if v is None or v == "":
            if f.get("required"):
                probs.append(f"缺少「{k}」")
            continue
        if t in TYPES and not (isinstance(v, TYPES[t]) and not (t == "number" and isinstance(v, bool))):
            probs.append(f"「{k}」要是 {t}")
        elif f.get("enum") and v not in f["enum"]:
            probs.append(f"「{k}」只能是：{'、'.join(map(str, f['enum']))}")
    if probs:
        raise Problem("；".join(probs))
    if not rid:
        rid = _new_id(new, spec)
        while os.path.exists(os.path.join(d, f"{rid}.json")):
            rid = f"{rid[:30]}-{uuid.uuid4().hex[:4]}"
    text = json.dumps(new, ensure_ascii=False, indent=2)
    if len(text.encode()) > DATA_MAX:
        raise Problem("内容太大了。")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{rid}.json")
    with open(p + ".tmp", "w", encoding="utf-8") as fp:
        fp.write(text)
    os.replace(p + ".tmp", p)
    commit(f"{app}：{'改了' if old else '加了'} {coll}/{rid}")
    return {**new, "id": rid}


def delete(app, coll, rid):
    _, d = _coll(app, coll)
    if not REC_ID.match(str(rid or "")):
        raise Problem("没有这条。")
    p = os.path.join(d, f"{rid}.json")
    if not os.path.isfile(p):
        raise Problem("没有这条。")
    os.remove(p)
    commit(f"{app}：删了 {coll}/{rid}")
    return {"ok": True}


def rename(app, name):
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise Problem("名字不能为空，最多 40 个字。")
    d = _dir(app)
    m = _meta(d) or {}
    m["name"] = name
    with open(os.path.join(d, "app.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    commit(f"{app}：改名为 {name}")
    return {"name": name}


def find(key):
    """按编号或名字（现在的名字，也认包含关系）找应用。"""
    key = (key or "").strip()
    rows = listing()
    for r in rows:
        if r["id"] == key or r["name"] == key:
            return r
    hits = [r for r in rows if key and (key in r["name"] or r["name"] in key)]
    return hits[0] if len(hits) == 1 else None


def describe(app):
    """给 Agent 看的：应用的名字、说明、数据接口、AGENT.md。"""
    m = meta(app)
    lines = [f"# {m.get('name') or app}（编号 {app}）", str(m.get("desc") or "")]
    for k, c in collections(app).items():
        c = c if isinstance(c, dict) else {}
        n = len(query(app, k))
        lines.append(f"\n## 集合 {k}：{c.get('name') or k}（{n} 条）{c.get('desc') or ''}")
        for fk, f in (c.get("fields") or {}).items():
            f = f if isinstance(f, dict) else {}
            extra = ("，必填" if f.get("required") else "") + (f"，只能是 {'/'.join(map(str, f['enum']))}" if f.get("enum") else "")
            lines.append(f"- {fk}（{f.get('type') or 'string'}{extra}）{f.get('desc') or ''}")
    if not collections(app):
        lines.append("\n（没有声明数据接口：数据在 data/ 下，看 AGENT.md）")
    try:
        lines.append("\n## AGENT.md\n" + open(os.path.join(_dir(app), "AGENT.md"), encoding="utf-8").read())
    except OSError:
        pass
    return "\n".join(lines)
