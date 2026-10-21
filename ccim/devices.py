"""设备管理（#100）：在网页里管某台电脑上的文件、清理磁盘。

每台电脑只处理自己的文件，网页请求按 X-Machine 转到那台电脑（和其他接口一样）。范围是用户主目录：
主目录外的不给看；主目录本身、里面的系统文件夹（桌面、文稿、资源库等）和 ccim 自己的目录不能删、不能改名。
删除一律移到废纸篓；磁盘清理里的缓存直接清空（能重新生成），清理只认最近一次扫描出来的路径。每次操作记一条（ops.jsonl）。
"""
import json, logging, os, platform as _platform, re, shutil, subprocess, threading, time

log = logging.getLogger("ccim.devices")

HOME = os.path.realpath(os.path.expanduser("~"))
FS_CHAT = "@fs"                     # 文件预览、下载时代替对话编号：不是对话里出现过的文件，按设备管理的范围放行
KEEP = {"Desktop", "Documents", "Downloads", "Library", "Pictures", "Movies", "Music", "Applications", "Public", ".Trash", ".ccim"}
HIDE_TOP = {"Library"}              # 主目录下默认不显示（访达也不显示），打开「显示隐藏文件」才有
LIST_MAX = 2000
DAY = 86400


class Problem(Exception):
    pass


# ---------- 范围 ----------

def _real(path):
    p = os.path.realpath(os.path.expanduser(path or "~"))
    if p != HOME and not p.startswith(HOME + os.sep):
        raise Problem("只能管理主目录里的文件。")
    return p


def allowed_file(path):
    """预览、下载：主目录里的普通文件。"""
    try:
        return os.path.isfile(_real(path))
    except Problem:
        return False


def _removable(p):
    if p == HOME or os.path.dirname(p) == HOME and os.path.basename(p) in KEEP:
        raise Problem(f"「{os.path.basename(p) or '主目录'}」不能删除或改名。")
    ccim = os.path.join(HOME, ".ccim")
    upload = re.match(re.escape(os.path.join(ccim, "projects")) + r"/[^/]+/uploads/[^/]+$", p)   # 网页里上传过的文件可以清理
    if (p == ccim or p.startswith(ccim + os.sep)) and not upload:
        raise Problem("ccim 自己的文件不能在这里删除或改名。")


def _no_access(p):
    return Problem("没有权限打开这个文件夹。到这台 Mac 的「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里，"
                   f"打开 ccim 用的 Python（{os.path.realpath(_python())}）。")


def _python():
    import sys
    return sys.executable


def _name_ok(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise Problem("名字不能为空，也不能有 /。")
    return name


# ---------- 概况 ----------

_model = None


def _model_name():
    global _model
    if _model is None:
        _model = ""
        try:
            out = subprocess.run(["system_profiler", "SPHardwareDataType"], capture_output=True, text=True, timeout=10).stdout
            kv = dict(line.strip().split(": ", 1) for line in out.splitlines() if ": " in line)
            _model = " · ".join(x for x in (kv.get("Model Name"), kv.get("Chip") or kv.get("Processor Name"),
                                             kv.get("Memory")) if x)
        except Exception:
            pass
    return _model


def overview(pdir):
    du = shutil.disk_usage(HOME)
    ver = _platform.mac_ver()[0]
    return {"model": _model_name(), "os": f"macOS {ver}" if ver else _platform.platform(),
            "home": HOME, "disk": {"total": du.total, "free": du.free, "used": du.total - du.free},
            "ops": recent_ops(pdir)}


# ---------- 文件 ----------

def ls(path, hidden=False):
    d = _real(path)
    if not os.path.isdir(d):
        raise Problem("找不到这个文件夹。")
    try:
        names = os.listdir(d)
    except PermissionError:
        raise _no_access(d)
    rows = []
    for n in names:
        if not hidden and (n.startswith(".") or d == HOME and n in HIDE_TOP):
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        isdir = os.path.isdir(p)
        rows.append({"name": n, "dir": isdir, "size": None if isdir else st.st_size, "mtime": st.st_mtime,
                     "link": os.path.islink(p)})
    rows.sort(key=lambda r: (not r["dir"], r["name"].lower()))
    return {"path": d, "home": HOME, "parent": os.path.dirname(d) if d != HOME else None,
            "entries": rows[:LIST_MAX], "more": max(0, len(rows) - LIST_MAX)}


def mkdir(pdir, path, name):
    d = _real(path)
    p = os.path.join(d, _name_ok(name))
    if os.path.exists(p):
        raise Problem("已经有同名的了。")
    try:
        os.mkdir(p)
    except PermissionError:
        raise _no_access(d)
    _op(pdir, "mkdir", [p])
    return {"path": p}


def rename(pdir, path, name):
    p = _real(path) if not os.path.islink(path) else os.path.abspath(path)
    _removable(p)
    q = os.path.join(os.path.dirname(p), _name_ok(name))
    if q == p:
        return {"path": p}
    if os.path.exists(q):
        raise Problem("已经有同名的了。")
    try:
        os.rename(p, q)
    except PermissionError:
        raise _no_access(os.path.dirname(p))
    _op(pdir, "rename", [p, q])
    return {"path": q}


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    q, i = os.path.join(folder, name), 2
    while os.path.lexists(q):
        q = os.path.join(folder, f"{base} {i}{ext}")
        i += 1
    return q


def _to_trash(p):
    trash = os.path.join(HOME, ".Trash")
    os.makedirs(trash, exist_ok=True)
    q = _unique(trash, os.path.basename(p))
    try:
        os.rename(p, q)
    except OSError:                       # 不在同一个卷上
        shutil.move(p, q)


def trash(pdir, paths):
    done, failed = [], []
    for raw in paths or []:
        try:
            p = os.path.abspath(raw) if os.path.islink(raw) else _real(raw)
            _real(os.path.dirname(p))
            _removable(p)
            if not os.path.lexists(p):
                raise Problem("已经不在了。")
            _to_trash(p)
            done.append(p)
        except PermissionError:
            failed.append({"path": raw, "error": "没有权限"})
        except (Problem, OSError) as e:
            failed.append({"path": raw, "error": str(e)})
    if done:
        _op(pdir, "trash", done)
    if failed and not done:
        raise Problem(failed[0]["error"])
    return {"done": done, "failed": failed}


def put(pdir, key, folder, name):
    """把网页传上来的文件（/api/upload 存在 uploads 里）放进这个文件夹。"""
    d = _real(folder)
    up = os.path.realpath(os.path.join(pdir, "uploads"))
    k = os.path.realpath(key or "")
    if not k.startswith(up + os.sep) or not os.path.isfile(k):
        raise Problem("文件没传上来，再试一次。")
    q = _unique(d, _name_ok(name))
    try:
        shutil.move(k, q)
    except PermissionError:
        raise _no_access(d)
    _op(pdir, "upload", [q])
    return {"path": q}


# ---------- 操作记录 ----------

def _op(pdir, op, paths, **extra):
    try:
        with open(os.path.join(pdir, "ops.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "op": op, "paths": paths, **extra}, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("记操作出错")


def recent_ops(pdir, n=20):
    try:
        with open(os.path.join(pdir, "ops.jsonl"), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------- 磁盘清理 ----------

MIN = 50 << 20                      # 小于这么大的不列
BIG = 1 << 30
OLD_BIG, OLD_DL, OLD_DEV, OLD_UP = 90 * DAY, 90 * DAY, 30 * DAY, 30 * DAY

# 能重新生成的缓存：(路径, 名字, 说明)
CACHES = [
    ("~/.cache/uv", "uv 缓存", ""), ("~/.cache/pip", "pip 缓存", ""), ("~/Library/Caches/pip", "pip 缓存", ""),
    ("~/.cache/huggingface", "Hugging Face 模型", "下载过的模型，删了用到时要重新下载"),
    ("~/.npm/_cacache", "npm 缓存", ""), ("~/Library/pnpm/store", "pnpm 缓存", ""), ("~/Library/Caches/Yarn", "Yarn 缓存", ""),
    ("~/.gradle/caches", "Gradle 缓存", ""), ("~/.cargo/registry", "Cargo 缓存", ""), ("~/go/pkg/mod", "Go 模块缓存", ""),
    ("~/Library/Developer/Xcode/DerivedData", "Xcode 编译缓存", ""),
    ("~/Library/Developer/Xcode/iOS DeviceSupport", "Xcode 设备支持文件", "连接设备时会重新生成"),
    ("~/Library/Developer/CoreSimulator/Caches", "模拟器缓存", ""),
]
# 依赖、编译结果：文件夹名 → 认得出来的标志（项目里的文件，或者它自己里面的文件）
ARTIFACTS = {"node_modules": ("package.json", None), ".venv": (None, "pyvenv.cfg"), "venv": (None, "pyvenv.cfg"),
             "target": ("Cargo.toml", None)}
SKIP = {"Library", ".Trash", ".ccim", ".git", "node_modules"}

GROUPS = [
    ("cache", "缓存", "能自动重新生成，直接清空"),
    ("dev", "开发产物", "很久没动的项目里的依赖和编译结果，需要时重新安装；移到废纸篓"),
    ("big", "大文件", "超过 1 GB、三个月没打开过；移到废纸篓"),
    ("downloads", "下载文件夹里的旧文件", "三个月没打开过；移到废纸篓"),
    ("trash", "废纸篓", "清空后找不回"),
    ("ccim", "ccim", "网页里上传过的旧文件；移到废纸篓"),
]


def _artifact(project, n):
    mark = ARTIFACTS.get(n)
    if not mark:
        return False
    beside, inside = mark
    return bool(beside and os.path.isfile(os.path.join(project, beside))
                or inside and os.path.isfile(os.path.join(project, n, inside)))


def _du(p, timeout=120):
    try:
        out = subprocess.run(["du", "-sk", p], capture_output=True, text=True, timeout=timeout).stdout
        return int(out.split()[0]) * 1024 if out.strip() else 0
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        return 0


def _used(st):
    return max(st.st_atime, st.st_mtime)


class Cleaner:
    """扫描在后台线程里跑；结果存 scan.json，清理只认里面的路径。"""

    def __init__(self, pdir):
        self.pdir = pdir
        self.file = os.path.join(pdir, "scan.json")
        self.lock = threading.Lock()
        self.job = {"state": "idle"}
        try:
            with open(self.file, encoding="utf-8") as f:
                self.result = json.load(f)
        except (OSError, ValueError):
            self.result = None

    def view(self):
        with self.lock:
            return {"home": HOME, "job": dict(self.job), "result": self.result, "groups": [{"id": g, "name": n, "desc": d} for g, n, d in GROUPS]}

    def start(self):
        with self.lock:
            if self.job.get("state") == "running":
                return self.view()
            self.job = {"state": "running", "step": "准备", "at": time.time(), "dirs": 0}
        threading.Thread(target=self._run, daemon=True).start()
        return self.view()

    def _step(self, step):
        with self.lock:
            self.job["step"] = step

    def _run(self):
        try:
            items = self._scan()
            res = {"at": time.time(), "items": items, "free": shutil.disk_usage(HOME).free, "denied": self.denied}
            with self.lock:
                self.result = res
                self.job = {"state": "done", "at": time.time()}
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            os.replace(tmp, self.file)
        except Exception as e:
            log.exception("扫描出错")
            with self.lock:
                self.job = {"state": "failed", "error": str(e), "at": time.time()}

    def _scan(self):
        now, items = time.time(), []

        def add(group, path, size, used=None, name=None, note=""):
            if size >= MIN or group == "trash" and size > 0:
                items.append({"group": group, "path": path, "size": size, "used": used,
                              "name": name or os.path.basename(path), "note": note})

        self._step("缓存")
        seen = set()
        for raw, name, note in CACHES:
            p = os.path.expanduser(raw)
            if os.path.isdir(p):
                seen.add(p)
                add("cache", p, _du(p), name=name, note=note)
        caches = os.path.join(HOME, "Library", "Caches")
        try:
            for n in sorted(os.listdir(caches)):
                p = os.path.join(caches, n)
                if p not in seen and os.path.isdir(p) and not os.path.islink(p):
                    add("cache", p, _du(p), name=f"{n} 的缓存")
        except OSError:
            pass

        self._step("废纸篓")
        add("trash", os.path.join(HOME, ".Trash"), _du(os.path.join(HOME, ".Trash")), name="废纸篓")

        self._step("下载文件夹")
        dl = os.path.join(HOME, "Downloads")
        try:
            for n in os.listdir(dl):
                p = os.path.join(dl, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if now - _used(st) < OLD_DL:
                    continue
                add("downloads", p, _du(p) if os.path.isdir(p) else st.st_size, _used(st))
        except OSError:
            pass

        self._step("ccim")
        up = os.path.join(self.pdir, "uploads")
        try:
            old = [os.path.join(up, n) for n in os.listdir(up)]
            old = [p for p in old if os.path.isfile(p) and now - os.path.getmtime(p) > OLD_UP]
            for p in old:
                add("ccim", p, os.path.getsize(p), os.path.getmtime(p), name=os.path.basename(p).split("-", 1)[-1])
        except OSError:
            pass

        self._step("主目录里的文件")
        denied = []
        for dirpath, dirs, files in os.walk(HOME, onerror=lambda e: denied.append(e.filename)):
            with self.lock:
                self.job["dirs"] = self.job.get("dirs", 0) + 1
            rel = os.path.relpath(dirpath, HOME)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            keep = []
            for n in dirs:
                p = os.path.join(dirpath, n)
                if os.path.islink(p):
                    continue
                if _artifact(dirpath, n):
                    used = self._project_used(dirpath, n)
                    if now - used > OLD_DEV:
                        add("dev", p, _du(p), used, name=f"{os.path.basename(dirpath)} / {n}")
                    continue
                if n in SKIP or n.startswith(".") or depth >= 8:
                    continue
                keep.append(n)
            dirs[:] = keep
            if dirpath == dl:                 # 下载文件夹上面单独算过
                dirs[:] = []
                continue
            for n in files:
                p = os.path.join(dirpath, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_size >= BIG and now - _used(st) > OLD_BIG and not os.path.islink(p):
                    add("big", p, st.st_size, _used(st))
        items.sort(key=lambda x: -x["size"])
        self.denied = len(denied)
        return items

    @staticmethod
    def _project_used(project, skip):
        """项目上次动过的时间：项目文件夹第一层里（除了依赖目录）最新的修改时间。"""
        latest = 0
        try:
            for n in os.listdir(project):
                if n == skip:
                    continue
                try:
                    latest = max(latest, os.lstat(os.path.join(project, n)).st_mtime)
                except OSError:
                    pass
        except OSError:
            pass
        return latest

    def clean(self, paths):
        with self.lock:
            known = {x["path"]: x for x in (self.result or {}).get("items", [])}
        done, failed, freed = [], [], 0
        for p in paths or []:
            x = known.get(p)
            if not x:
                failed.append({"path": p, "error": "不在扫描结果里，重新扫描一次"})
                continue
            try:
                if x["group"] in ("cache", "trash"):
                    _empty(p)
                else:
                    _real(p)
                    _removable(p)
                    if os.path.lexists(p):
                        _to_trash(p)
                done.append(p)
                freed += x["size"]
            except PermissionError:
                failed.append({"path": p, "error": "没有权限"})
            except (Problem, OSError) as e:
                failed.append({"path": p, "error": str(e)})
        if done:
            _op(self.pdir, "clean", done, freed=freed)
            with self.lock:
                if self.result:
                    self.result["items"] = [x for x in self.result["items"] if x["path"] not in done]
        return {"done": done, "failed": failed, "freed": freed}


def _empty(folder):
    """清空文件夹里的内容，文件夹本身留着（应用还认它）。"""
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
