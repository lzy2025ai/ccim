"""日记（#363）：在任何对话里发 /diary 内容，记到本机的 Markdown 日记里，不交给助手。

按天一个文件（YYYY-MM-DD.md，和 Obsidian 日记插件默认的文件名一样），放在「文稿 / 文档」下的 ccim-日记，
整个文件夹可以直接当 Obsidian 库打开。每条是「## 时间 · 记在哪」加正文，用户在 Obsidian 里改了也照样能读、能搜。
助手用 diary_read / diary_search 两个工具查（session.py 的 _diary_tools）。
"""
import datetime, os, re

CMD = re.compile(r"^/(diary|日记)(?=\s|$)\s*", re.I)
HEAD = re.compile(r"^## (\d{1,2}:\d{2})(?: · (.*))?$")
READ_DAYS = 62            # diary_read 一次最多读几天
READ_MAX = 40000          # 一次最多给助手多少字
SEARCH_MAX = 40           # 搜索最多给几条


def root():
    # Windows 的「文档」也在用户目录下的 Documents
    return os.path.join(os.path.expanduser("~"), "Documents", "ccim-日记")


def parse(text):
    """「/diary 内容」→ 内容；不是 /diary 返回 None（内容为空返回 ""）。"""
    m = CMD.match((text or "").lstrip())
    return (text or "").lstrip()[m.end():].strip() if m else None


def add(text, where, now=None):
    """记一条，返回 {date, time, where, path}。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("在 /diary 后面写上要记的内容。")
    now = now or datetime.datetime.now()
    d, t = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    os.makedirs(root(), exist_ok=True)
    f = os.path.join(root(), d + ".md")
    where = " ".join((where or "").split())
    new = not os.path.exists(f) or os.path.getsize(f) == 0
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(("" if new else "\n") + f"## {t}" + (f" · {where}" if where else "") + f"\n\n{text}\n")
    return {"date": d, "time": t, "where": where, "path": f}


def _entries(d):
    """某一天的日记：[{time, where, text}]。开头不在任何「## 时间」下面的内容算一条没有时间的。"""
    try:
        with open(os.path.join(root(), d + ".md"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out, cur = [], {"time": "", "where": "", "lines": []}
    for ln in lines:
        m = HEAD.match(ln)
        if m:
            out.append(cur)
            cur = {"time": m.group(1), "where": m.group(2) or "", "lines": []}
        else:
            cur["lines"].append(ln)
    out.append(cur)
    return [{"time": e["time"], "where": e["where"], "text": "\n".join(e["lines"]).strip()}
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


def _fmt(d, e):
    head = " · ".join(x for x in (d + (" " + e["time"] if e["time"] else ""), e["where"]) if x)
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
