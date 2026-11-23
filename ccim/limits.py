"""额度用完停下的对话（#563）：认出报错、算出几点刷新，刷新后由平台错开着一个个唤醒、接着做。

报错是 Claude Code 给的英文，例：「You've hit your 5-hour limit · resets 1:20am (Asia/Shanghai)」，
也可能套在别的错误里（「Prompt is too long · automatic compaction failed: You've hit your …」）。
刷新时间先按报错里写的算；认不出就用最近一次 RateLimitEvent 里已经用满的那个窗口的 resetsAt；再没有就半小时后再试。
在等的对话在对话状态里记 limit = {until, at}：重启后照样唤醒；人在这期间自己接着聊了（新的一轮开始）就清掉，不再唤醒。
"""
import datetime, re, time

from . import usage

HIT = re.compile(r"hit your [\w -]{0,30}?limit|usage limit (?:reached|exceeded)", re.I)
WHEN = re.compile(r"(?:resets|try again)\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                  r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?\s*(?:\((?P<tz>[^)]+)\))?", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
FALLBACK = 1800                               # 认不出刷新时间：半小时后试一次，又撞上就再等
MARGIN = 60                                   # 刷新时间过了再等一会儿
STAGGER = 120                                 # 好几个对话在等：一个一个放，间隔这么久
KEEP = 8 * 86400                              # 等了这么久还没唤醒（比如这台电脑一直关着）就算了
RESUME = ("[刚才这一轮因为 Claude 额度用完被打断了，现在额度已经刷新，接着做原来的事。"
          "先看一眼刚才做到哪了（文件、命令的结果），不要从头重来；做完照常把结果写清楚]")


def hit(error):
    return bool(error) and bool(HIT.search(str(error)))


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip()) if name else None
    except Exception:
        return None


def _parse(m, now):
    tz = _tz(m.group("tz"))
    base = datetime.datetime.fromtimestamp(now, tz) if tz else datetime.datetime.fromtimestamp(now)
    h, mi, ap = int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    day = base.date()
    if m.group("mon"):
        mon = m.group("mon")[:3].lower()
        if mon not in MONTHS:
            return None
        try:
            day = datetime.date(base.year, MONTHS.index(mon) + 1, int(m.group("day")))
        except ValueError:
            return None
    at = datetime.datetime(day.year, day.month, day.day, h, mi, tzinfo=tz).timestamp()
    if m.group("mon"):
        if at < now - 86400:                  # 年底写的是明年一月
            at = datetime.datetime(day.year + 1, day.month, day.day, h, mi, tzinfo=tz).timestamp()
    elif at < now - 300:                      # 只写了几点：已经过了就是明天这个点
        at += 86400
    return at


def until(error, now=None):
    """几点刷新（时间戳）。"""
    now = now or time.time()
    for m in WHEN.finditer(str(error or "")):
        at = _parse(m, now)
        if at:
            return at
    wins = ((usage.cached() or {}).get("windows") or {}).values()
    full = [w["resetsAt"] for w in wins if isinstance(w, dict) and (w.get("resetsAt") or 0) > now
            and (w.get("status") not in (None, "allowed", "allowed_warning") or (w.get("utilization") or 0) >= 1)]
    return max(full) if full else now + FALLBACK


def clock(ts, now=None):
    now = now or time.time()
    t, n = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%H:%M", t)
    if t[:3] == n[:3]:
        return hm
    if t[:3] == time.localtime(now + 86400)[:3]:
        return "明天 " + hm
    return f"{t.tm_mon} 月 {t.tm_mday} 日 {hm}"


def text(ts, auto=True):
    """给人看的一句：额度用完了，01:20 后自动继续。"""
    return f"额度用完了，{clock(ts)} 后自动继续" if auto else f"额度用完了，{clock(ts)} 刷新后可以接着聊"


def due(waiting, last=0, now=None):
    """该放哪一个：waiting 是 [(对话, limit)]。所有在等的对话共用一个账号的额度，所以要等最晚的那个刷新时间过了
    （刚放出去的又撞上限，会把它推后，后面的接着等）；先停下的先放；离上一个至少 STAGGER 秒。"""
    now = now or time.time()
    if not waiting or now - last < STAGGER:
        return None
    gate = max(lim.get("until") or 0 for _, lim in waiting)
    if now < gate + MARGIN:
        return None
    return min(waiting, key=lambda x: x[1].get("at") or 0)[0]
