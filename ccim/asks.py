"""ccim ask：agent 给项目里的 Claude Code / Codex 派任务。

和 handoff 一样走状态目录里的文件：命令行写请求 → 正在运行的 ccim 进程每秒取一次，在对应的聊天里发出任务、
排进那个对话的队列 → 写「已接下」（带进程号）→ 做完写结果。命令行等结果时也看进程还在不在，进程没了就不再等。
一个任务三个文件：<编号>.json 请求（取走后删掉）、<编号>.status.json、<编号>.result.json；放在 ccim 进程登记的
状态目录（飞书是项目自己的，微信是那个微信号的）下的 asks/。
"""
import glob, json, os, time, uuid

from . import registry

KEEP_DAYS = 7


def _dir(home):
    d = os.path.join(registry.pdir(home), "asks")
    os.makedirs(d, exist_ok=True)
    return d


def _write(path, data):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------- 命令行这边 ----------

def submit(home, req):
    tid = uuid.uuid4().hex[:10]
    _write(os.path.join(_dir(home), f"{tid}.json"), dict(req, id=tid, at=registry.now()))
    return tid


def status(home, tid):
    return _read(os.path.join(_dir(home), f"{tid}.status.json"))


def result(home, tid):
    return _read(os.path.join(_dir(home), f"{tid}.result.json"))


def find(tid):
    """按任务编号找它在哪个状态目录（ccim ask --result 用）。返回 asks 目录，找不到返回 None。"""
    hits = glob.glob(os.path.join(registry.HOME, "projects", "*", "asks", f"{tid}.status.json"))
    return os.path.dirname(hits[0]) if hits else None


def read_in(d, tid, kind):
    return _read(os.path.join(d, f"{tid}.{kind}.json"))


# ---------- ccim 进程这边 ----------

def take(home):
    """取走所有待处理的请求。顺便清掉一周前的旧文件。"""
    d = _dir(home)
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json")), key=os.path.getmtime):
        name = os.path.basename(p)
        if name.count(".") != 1:              # 只要 <编号>.json，不碰 status / result
            continue
        req = _read(p)
        os.remove(p)
        if req:
            out.append(req)
    if out:
        cutoff = time.time() - KEEP_DAYS * 86400
        for p in glob.glob(os.path.join(d, "*.json")):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    return out


def mark(home, tid, **data):
    _write(os.path.join(_dir(home), f"{tid}.status.json"), dict(data, pid=os.getpid(), at=registry.now()))


def done(home, tid, state, answer):
    _write(os.path.join(_dir(home), f"{tid}.result.json"), {"state": state, "answer": answer, "at": registry.now()})
