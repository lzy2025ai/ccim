"""性能数据（#286）：接口和网页动作的耗时，只存在这台电脑上，开发页里看。

每条一行 JSON 记在 ~/.ccim/perf/perf.jsonl，按大小滚动（留 KEEP 份）。
慢的（超过 SLOW 秒）多记现场：参数、同时在处理几个请求，以及还没做完时它卡在哪段代码（看门狗抓的调用栈），并写一行日志方便对照。
"""
import json, logging, os, sys, threading, time, uuid
from collections import Counter

from . import registry

log = logging.getLogger("ccim.perf")
SLOW = 1.0                      # 秒：超过就算慢，抓现场
STACK_AT = (1.0, 3.0, 10.0)     # 还没做完时，到这几个时间点各抓一次调用栈
MAX_BYTES, KEEP = 5 << 20, 3    # 每份 5MB，留 3 份（当前 + 2 份旧的）
WEB_KINDS = {"load", "state", "chat", "api"}   # 网页报回来的几种

_lock = threading.Lock()
_flight = {}                    # 进行中的请求：编号 → {name, start, thread, stacks}
_watch = None


def _dir():
    return os.path.join(registry.HOME, "perf")


def _file(i=0):
    return os.path.join(_dir(), "perf.jsonl" + (f".{i}" if i else ""))


def _write(rec):
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        try:
            os.makedirs(_dir(), exist_ok=True)
            p = _file()
            if os.path.exists(p) and os.path.getsize(p) > MAX_BYTES:
                for i in range(KEEP - 1, 0, -1):
                    if os.path.exists(_file(i - 1)):
                        os.replace(_file(i - 1), _file(i))
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            log.debug("性能数据写不进去", exc_info=True)


def _stack(thread_id):
    """那个线程现在在哪：ccim 自己的代码，加上最里面那一层（卡在读文件、等网络时是标准库）。"""
    frame = sys._current_frames().get(thread_id)
    rows = []
    while frame:
        c = frame.f_code
        rows.append((c.co_filename, frame.f_lineno, c.co_name))
        frame = frame.f_back
    rows.reverse()
    if not rows:
        return []
    mine = lambda p: f"{os.sep}ccim{os.sep}" in p
    keep = [r for r in rows if mine(r[0]) and not r[0].endswith(f"{os.sep}perf.py")]
    if rows[-1] not in keep:
        keep.append(rows[-1])
    name = lambda p: p.split(f"{os.sep}ccim{os.sep}", 1)[-1] if mine(p) else "(lib) " + os.path.basename(p)
    return [f"{name(f)}:{n} {fn}" for f, n, fn in keep[-12:]]


def _spot(stack):
    """一个调用栈落在哪：最里面那层 ccim 的代码，卡在标准库里的再带上那一层。"""
    own = [s for s in stack if not s.startswith("(lib) ")]
    if not own:
        return stack[-1] if stack else ""
    return own[-1] + (" → " + stack[-1][6:] if stack[-1].startswith("(lib) ") else "")


def _watchdog():
    while True:
        time.sleep(0.25)
        now = time.monotonic()
        with _lock:
            items = list(_flight.values())
        for it in items:
            took = now - it["start"]
            if len(it["stacks"]) < len(STACK_AT) and took >= STACK_AT[len(it["stacks"])]:
                try:
                    it["stacks"].append({"at": round(took, 1), "stack": _stack(it["thread"])})
                except Exception:
                    it["stacks"].append({"at": round(took, 1), "stack": []})


def _start_watch():
    global _watch
    if _watch is None:
        with _lock:
            if _watch is None:
                _watch = threading.Thread(target=_watchdog, name="ccim-perf", daemon=True)
                _watch.start()


def _short(v, n=80):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def ctx_of(q=None, body=None):
    """记下来帮忙定位的参数：GET 的查询参数；POST 只记有哪些字段和几个编号类的值，不记内容。"""
    out = {}
    if q:
        out["q"] = {k: _short(v) for k, v in q.items()}
    if isinstance(body, dict):
        out["keys"] = sorted(body)[:20]
        ids = {k: _short(body[k], 60) for k in ("id", "chat", "agent", "machine", "name", "date", "view") if isinstance(body.get(k), str)}
        if ids:
            out["ids"] = ids
    return out


class Track:
    """包住一个请求：with perf.Track("api", "/api/state", via=…) as t: …；t.status、t.size 做完前填上。"""

    def __init__(self, kind, name, **ctx):
        self.kind, self.name, self.ctx = kind, name, ctx
        self.status = self.size = None

    def __enter__(self):
        _start_watch()
        self.id = uuid.uuid4().hex[:8]
        self.t0 = time.monotonic()
        self.it = {"name": self.name, "start": self.t0, "thread": threading.get_ident(), "stacks": []}
        with _lock:
            self.busy = len(_flight)               # 进来时已经有几个在处理
            _flight[self.id] = self.it
        return self

    def took(self):
        return (time.monotonic() - self.t0) * 1000

    def __exit__(self, et, ev, tb):
        with _lock:
            _flight.pop(self.id, None)
        ms = self.took()
        rec = {"at": round(time.time(), 3), "k": self.kind, "n": self.name, "ms": round(ms, 1)}
        if self.status is not None:
            rec["st"] = self.status
        if et is not None:
            rec["st"] = rec.get("st") or "error"
        if ms >= SLOW * 1000:
            rec.update(id=self.id, busy=self.busy, **{k: v for k, v in self.ctx.items() if v not in (None, "", {}, [])})
            if self.size is not None:
                rec["size"] = self.size
            if self.it["stacks"]:
                rec["stacks"] = self.it["stacks"]
            log.info("慢请求 %s %s %.0fms（同时 %s 个）[%s]", self.kind, self.name, ms, self.busy, self.id)
        _write(rec)
        return False


def web(items, machine=None):
    """网页报回来的耗时：[{k, n, ms, server?, ctx?}]。"""
    now = round(time.time(), 3)
    for x in (items or [])[:200]:
        if not isinstance(x, dict) or x.get("k") not in WEB_KINDS:
            continue
        try:
            ms = float(x.get("ms"))
        except (TypeError, ValueError):
            continue
        if not 0 <= ms < 600000:
            continue
        rec = {"at": now, "k": "web." + x["k"], "n": _short(str(x.get("n") or ""), 120), "ms": round(ms, 1)}
        if isinstance(x.get("server"), (int, float)):
            rec["server"] = round(float(x["server"]), 1)
        if ms >= SLOW * 1000:
            rec["id"] = uuid.uuid4().hex[:8]
            if isinstance(x.get("ctx"), dict):
                rec.update({k: _short(v) for k, v in list(x["ctx"].items())[:10]})
            if machine:
                rec["from"] = _short(machine, 40)
        _write(rec)


def _read(since):
    rows = []
    for i in range(KEEP - 1, -1, -1):
        try:
            with open(_file(i), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("at", 0) >= since:
                        rows.append(r)
        except OSError:
            pass
    return rows


def _pct(xs, p):
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def summary(hours=24, slow=30):
    """开发页：每个（种类, 名字）的次数、中位数、九成在多少以内、最长；最近最慢的几次（带现场）。"""
    hours = max(1, min(int(hours or 24), 24 * 14))
    since = time.time() - hours * 3600
    rows = _read(since)
    groups = {}
    for r in rows:
        groups.setdefault((r.get("k", ""), r.get("n", "")), []).append(r)
    out = []
    for (k, n), rs in groups.items():
        ms = sorted(r["ms"] for r in rs)
        g = {"k": k, "n": n, "count": len(rs), "p50": _pct(ms, .5), "p90": _pct(ms, .9), "max": ms[-1],
             "slow": sum(1 for x in ms if x >= SLOW * 1000),
             "errors": sum(1 for r in rs if isinstance(r.get("st"), int) and r["st"] >= 500 or r.get("st") == "error")}
        sv = sorted(r["server"] for r in rs if "server" in r)
        if sv:
            g["server_p50"] = _pct(sv, .5)
        out.append(g)
    out.sort(key=lambda g: -(g["p90"] * g["count"]))   # 总共耗掉的时间多的排前面
    slowest = sorted((r for r in rows if r.get("ms", 0) >= SLOW * 1000), key=lambda r: -r["ms"])[:slow]
    where = Counter()
    for r in rows:
        for s in r.get("stacks") or []:
            if s.get("stack"):
                where[_spot(s["stack"])] += 1
    return {"hours": hours, "total": len(rows), "slow_ms": SLOW * 1000, "groups": out[:80], "slowest": slowest,
            "hotspots": [{"at": w, "n": c} for w, c in where.most_common(10)]}
