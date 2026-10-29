"""应用（#103）：一个应用 = 一个页面 + 一个数据目录 + 给 Agent 的说明，放在 ~/.ccim/apps/<编号>/。

Agent 照 README（GUIDE）直接在这台电脑上建、改应用、往 data/ 里写数据；网页把页面放进隔离的 iframe，
页面只能通过 window.ccim（网页转成下面这几个接口）读写自己的 data/。整个目录是一个 git 仓库，每次改动一个提交，可以退回。
"""
import json
import os
import re
import shutil
import subprocess

from . import registry

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PAGE_MAX = 2 * 1024 * 1024        # index.html 最大多少（页面整个放进 srcdoc）
DATA_MAX = 5 * 1024 * 1024        # 一次写入最多多少
GUIDE = """\
# ccim 应用

用户在 ccim 网页左侧「应用」里打开这里的应用。一个应用一个文件夹，文件夹名就是编号（小写字母、数字、横线）：

```
<编号>/
  app.json      {"name": "竞品记录", "desc": "一句话说明", "icon": "🔍", "agent": "做这个应用的 Agent 编号（可空）"}
  index.html    页面，所有样式和脚本写在这一个文件里
  AGENT.md      给 Agent 的说明：数据怎么存、每个字段什么意思、往里加东西要注意什么
  data/         数据，页面和 Agent 共用
```

## 页面

页面跑在隔离的框里，不能直接访问平台和本机文件，只能用平台给的 `window.ccim`（都返回 Promise）：

| 调用 | 作用 |
|---|---|
| `ccim.list(dir?)` | 列出 data/ 下（或 data/<dir>/ 下）的文件：`[{path, size, mtime}]` |
| `ccim.read(path)` | 读 data/<path> 的文本；不存在返回 null |
| `ccim.readJSON(path, fallback)` | 读并解析 JSON |
| `ccim.write(path, text)` / `ccim.writeJSON(path, value)` | 写 data/<path>（目录自动建） |
| `ccim.remove(path)` | 删 data/<path> |
| `ccim.copy(text, html?)` | 复制到剪贴板；给了 html 就带格式复制（贴到公众号编辑器等） |
| `ccim.chat(text)` | 打开一个和做这个应用的 Agent 的新对话，输入框里先填好 text |

数据变了（Agent 写了新东西）会自动通知页面：`ccim.onChange(fn)`。
页面要配合平台的浅色界面：白底、深灰字、圆角、留白；手机上也要能用（宽度 360 起）。不要用浏览器原生的下拉框、alert、confirm。

## Agent 往里加东西

数据就是 data/ 下的普通文件，直接读写（建议一条记录一个 JSON 文件，或一个 JSON 数组文件，写在 AGENT.md 里）。
改之前先读那个应用的 AGENT.md。平台会把这个目录的改动自动提交到 git，做坏了可以用 git 退回。
做好、改好之后告诉用户在「应用」里打开哪个。
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
            "sig": data_sig(app)}


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
    """data/ 和页面的变化标记：网页每隔几秒问一次，变了就通知页面（Agent 写进了新数据）。"""
    d = _dir(app)
    return str(round(_updated(d), 3))
