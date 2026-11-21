"""功能使用（#533）：网页报回来打开了哪些页面、点了哪些关键操作，只存在这台电脑上，开发页里看哪些常用、哪些基本没用。

一个月一个文件 ~/.ccim/usage/usage-YYYY-MM.jsonl，一行 {"at", "f"}；只留最近 KEEP_DAYS 天（按月删整份）。
功能有哪些、叫什么由网页定（它知道有哪些页面和按钮），这里只记编号、按编号统计。
"""
import datetime as dt, json, os, re, threading, time

from . import registry

KEEP_DAYS = 90
_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,59}$")
_lock = threading.Lock()


def _dir():
    return os.path.join(registry.HOME, "usage")


def record(items, machine=None):
    """items：[{f, at?}]，at 是网页那边的时间（秒），没有就用现在。"""
    now = time.time()
    rows = []
    for x in (items or [])[:300]:
        f = x.get("f") if isinstance(x, dict) else None
        if not isinstance(f, str) or not _KEY.match(f):
            continue
        at = x.get("at")
        at = float(at) if isinstance(at, (int, float)) and now - 86400 < at <= now + 60 else now
        rows.append({"at": round(at, 1), "f": f})
    if not rows:
        return
    os.makedirs(_dir(), exist_ok=True)
    with _lock:
        for r in rows:
            name = dt.datetime.fromtimestamp(r["at"]).strftime("usage-%Y-%m.jsonl")
            with open(os.path.join(_dir(), name), "a", encoding="utf-8") as fp:
                fp.write(json.dumps(r) + "\n")
        _prune()


def _prune():
    cut = (dt.date.today() - dt.timedelta(days=KEEP_DAYS + 31)).strftime("usage-%Y-%m.jsonl")
    try:
        for name in os.listdir(_dir()):
            if name.startswith("usage-") and name < cut:
                os.remove(os.path.join(_dir(), name))
    except OSError:
        pass


def summary():
    """每个功能：近 7 天、近 30 天几次，最后一次的时间，最早有记录的时间（判断「从没用过」是不是记得够久）。"""
    now = time.time()
    out, first = {}, None
    try:
        names = sorted(n for n in os.listdir(_dir()) if n.startswith("usage-"))
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(_dir(), name), encoding="utf-8") as fp:
                for line in fp:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    at, f = r.get("at") or 0, r.get("f")
                    if not f or now - at > KEEP_DAYS * 86400:
                        continue
                    first = at if first is None else min(first, at)
                    s = out.setdefault(f, {"d7": 0, "d30": 0, "d90": 0, "last": 0})
                    s["d90"] += 1
                    if now - at <= 30 * 86400:
                        s["d30"] += 1
                    if now - at <= 7 * 86400:
                        s["d7"] += 1
                    s["last"] = max(s["last"], at)
        except OSError:
            continue
    return {"features": out, "since": first}
