"""新电脑一条命令接入（#175）：总控提供 join.sh 和它自己正在跑的这个版本的 ccim 安装包。

    curl -fsSL https://<总控>/join.sh | sh -s <配对码>

安装包不经过 GitHub：总控把自己的 ccim 代码现打成 wheel（纯 Python，自己写 zip，不需要构建工具），
版本号带上提交（0.19.0+g8c1de9e），装好后 `upgrade.info` 从版本号认出提交和来源，网页上照常能升级。
join.sh 是 ccim/join.sh 模板，填上总控地址、版本和安装包的校验值再给出去。
"""
import base64, hashlib, importlib.metadata as md, os, re, threading, time, zipfile

from . import registry

PKG = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__"}
_lock = threading.Lock()


def _project():
    """名字、版本、依赖、入口：开发版读代码目录里的 pyproject.toml（装的时候的记录可能是旧的），否则读安装记录。"""
    pp = os.path.join(os.path.dirname(PKG), "pyproject.toml")
    if os.path.exists(pp):
        import tomllib
        with open(pp, "rb") as f:
            p = tomllib.load(f)["project"]
        return {"version": p["version"], "summary": p.get("description", ""), "python": p.get("requires-python", ">=3.11"),
                "deps": p.get("dependencies", []), "scripts": p.get("scripts", {})}
    d = md.distribution("ccim")
    return {"version": d.version.split("+")[0], "summary": d.metadata.get("Summary", ""),
            "python": d.metadata.get("Requires-Python", ">=3.11"), "deps": [r for r in d.requires or [] if "extra ==" not in r],
            "scripts": {e.name: e.value for e in d.entry_points if e.group == "console_scripts"}}


def commit():
    """总控正在跑的提交（7 位）；认不出来返回空。"""
    from . import upgrade
    c = upgrade.info().get("commit") or ""
    return c if re.fullmatch(r"[0-9a-f]{7,40}", c) else ""


def version():
    """装到新电脑上的版本号：0.19.0+g8c1de9e。"""
    v, c = _project()["version"], commit()
    return f"{v}+g{c[:7]}" if c else v


def _files():
    for root, dirs, names in os.walk(PKG):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            if n.startswith(".") or n.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, n)
            yield full, "ccim/" + os.path.relpath(full, PKG).replace(os.sep, "/")


def _digest(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _dist_dir():
    return os.path.join(registry.HOME, "dist")


def wheel():
    """打好的安装包：(路径, 文件名, sha256)。按版本缓存，代码目录里有文件比它新就重打（开发版没提交的改动）。"""
    p = _project()
    ver = version()
    name = f"ccim-{ver}-py3-none-any.whl"
    path = os.path.join(_dist_dir(), name)
    with _lock:
        newest = max(os.path.getmtime(f) for f, _ in _files())
        if not os.path.exists(path) or os.path.getmtime(path) < newest:
            os.makedirs(_dist_dir(), exist_ok=True)
            info = f"ccim-{ver}.dist-info"
            meta = "\n".join(["Metadata-Version: 2.1", "Name: ccim", f"Version: {ver}", f"Summary: {p['summary']}",
                              f"Requires-Python: {p['python']}"] + [f"Requires-Dist: {d}" for d in p["deps"]]) + "\n"
            extra = {f"{info}/METADATA": meta.encode(),
                     f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: ccim\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                     f"{info}/entry_points.txt": ("[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in p["scripts"].items())).encode()}
            record = []
            tmp = path + ".tmp"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for full, arc in _files():
                    with open(full, "rb") as f:
                        data = f.read()
                    z.writestr(arc, data)
                    record.append(f"{arc},{_digest(data)},{len(data)}")
                for arc, data in extra.items():
                    z.writestr(arc, data)
                    record.append(f"{arc},{_digest(data)},{len(data)}")
                record.append(f"{info}/RECORD,,")
                z.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
            os.replace(tmp, path)
            for old in os.listdir(_dist_dir()):              # 留最近几个：网页升级失败时装回原来的版本要用
                if old.endswith(".whl") and old != name and time.time() - os.path.getmtime(os.path.join(_dist_dir(), old)) > 30 * 86400:
                    os.remove(os.path.join(_dist_dir(), old))
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
    return path, name, sha


def wheel_file(name):
    """/peer/dl/<文件名>：只给当前这个版本的安装包。"""
    path, cur, _ = wheel()
    return path if name == cur else None


def script(base):
    """填好的 join.sh。base：总控对外的地址（https://IP，或局域网里的 http://IP:8978）。"""
    _, name, sha = wheel()
    with open(os.path.join(PKG, "join.sh"), encoding="utf-8") as f:
        s = f.read()
    for k, v in {"HUB": base.rstrip("/"), "VERSION": version(), "COMMIT": commit()[:7], "WHEEL": name, "SHA256": sha}.items():
        s = s.replace("@@" + k + "@@", v)
    return s.encode()


# ---------- 进度：网页上「添加电脑」实时显示那台电脑走到哪一步了 ----------

STEPS = ("started", "installing", "joining", "done", "failed")


def progress_event(rec, body):
    """join.sh / ccim join 报的进度 → 推给网页的事件。rec 是配对码的记录（peers.codes 里的），没有 pid 的不推。"""
    step = str(body.get("step") or "")
    if step not in STEPS or not rec.get("pid"):
        return None
    ev = {"type": "pairing", "pid": rec["pid"], "step": step, "_acct": rec.get("account") or ""}
    for k in ("name", "error"):
        if body.get(k):
            ev[k] = str(body[k])[:300]
    return ev

