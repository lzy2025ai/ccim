"""输入框里打 / 时列出的 Skill（#381）。

名单以 Claude 会话开头报的为准（init 里的 skills，含 Claude Code 自带的、启用的插件里的），说明取会话的命令表（get_server_info），
按工作目录记在 ~/.ccim/slash.json；缺说明的从各处的 SKILL.md、commands/*.md 里读。还没在这个目录开过会话的，用最近一次报的名单，再扫一遍全局、项目目录补上。
"""
import json, os, re, threading

from . import registry

LOCK = threading.Lock()
KEEP = 40                             # 最多记这么多个目录
ORDER = {"项目": 0, "本机": 1, "平台": 2, "插件": 3, "Claude Code": 4}


def _cache_path():
    return os.path.join(registry.HOME, "slash.json")


def _load():
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(cwd, data, commands=None):
    """Claude 会话开头的 init：记下这个目录能用的 Skill 名单、插件目录；commands 是会话的命令表，取里面的说明。"""
    names = [n for n in (data or {}).get("skills") or [] if isinstance(n, str)]
    if not names:
        return
    plugins = [{"name": p.get("name"), "path": p.get("path")} for p in (data or {}).get("plugins") or []
               if isinstance(p, dict) and p.get("name") and p.get("path") and os.path.isabs(p["path"])]
    desc = {}
    for c in commands or []:
        if isinstance(c, dict) and c.get("name") in names and c.get("description"):
            d = re.sub(r"\s*\((user|project)\)\s*$", "", re.sub(r"^\([^)]*\)\s*", "", c["description"]))
            desc[c["name"]] = _short(d)
    entry = {"skills": names, "plugins": plugins, "desc": desc}
    with LOCK:
        cur = _load()
        if cur.get(cwd or "") == entry and cur.get("*") == entry:
            return
        cur.pop(cwd or "", None)
        cur[cwd or ""] = entry
        cur["*"] = entry                  # 新目录、还没开过会话的先用它
        keys = [k for k in cur if k != "*"]
        for k in keys[:-KEEP]:
            cur.pop(k, None)
        tmp = _cache_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False)
            os.replace(tmp, _cache_path())
        except OSError:
            pass


def _short(desc):
    """说明只留第一句，列表里一行放得下。"""
    d = " ".join((desc or "").split())
    m = re.search(r"[。！？]|\.\s|\.$", d)
    d = d[:m.end()].rstrip("。. ") if m else d
    return d[:90]


def _front(path):
    from .skills import _front as front
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(8000)
    except OSError:
        return {}
    out = front(text)
    if "description" not in out:      # 自定义命令没写说明的，用正文第一行
        body = re.sub(r"\A\s*---.*?\n---", "", text, flags=re.S)
        first = next((x.strip("# ").strip() for x in body.splitlines() if x.strip()), "")
        if first:
            out["description"] = first
    return out


def _scan(base, src, found, prefix=""):
    """base 下的 skills/<名字>/SKILL.md 和 commands/<名字>.md。"""
    sk = os.path.join(base, "skills")
    try:
        for n in sorted(os.listdir(sk)):
            p = os.path.join(sk, n, "SKILL.md")
            if os.path.isfile(p) and prefix + n not in found:
                found[prefix + n] = {"desc": _short(_front(p).get("description")), "src": src}
    except OSError:
        pass
    cm = os.path.join(base, "commands")
    try:
        for n in sorted(os.listdir(cm)):
            if n.endswith(".md") and prefix + n[:-3] not in found:
                found[prefix + n[:-3]] = {"desc": _short(_front(os.path.join(cm, n)).get("description")), "src": src}
    except OSError:
        pass


def _enabled_plugins():
    """还没开过会话时：按 Claude Code 的设置找启用的插件目录。"""
    home = os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(home, "settings.json"), encoding="utf-8") as f:
            on = {k for k, v in (json.load(f).get("enabledPlugins") or {}).items() if v}
        with open(os.path.join(home, "plugins", "installed_plugins.json"), encoding="utf-8") as f:
            inst = json.load(f).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for key in on:
        rows = inst.get(key) or []
        if rows and isinstance(rows, list) and rows[0].get("installPath"):
            out.append({"name": key.split("@")[0], "path": rows[0]["installPath"]})
    return out


def listing(cwd="", agent=None, guest=None):
    """[{name, desc, src}]：这个目录、这个 Agent 的会话里能用的 Skill。"""
    from . import skills
    cache = _load()
    entry = cache.get(cwd or "") or cache.get("*") or {}
    found = {}
    if cwd and not guest:
        _scan(os.path.join(cwd, ".claude"), "项目", found)
    _scan(os.path.expanduser("~/.claude"), "本机", found)
    for p in entry.get("plugins") or _enabled_plugins():
        if p["name"] != skills.PLUGIN:
            _scan(p["path"], "插件", found, p["name"] + ":")
    off = set((agent or {}).get("skills_off") or [])
    tok = skills.SPACE.set(None)
    try:
        mine = [m for m in skills.listing() if not m.get("disabled") and m["name"] not in off]
        if guest:
            skills.SPACE.set(guest)
            mine += [m for m in skills.listing() if not m.get("disabled") and m["name"] not in off]
    finally:
        skills.SPACE.reset(tok)
    for m in mine:
        if m["name"] not in found:        # 本机装了同名的就只列本机那个
            found[f"{skills.PLUGIN}:{m['name']}"] = {"desc": _short(m.get("desc")), "src": "平台"}
    names, said = set(entry.get("skills") or []), entry.get("desc") or {}
    for n in names:
        if n not in found and not n.startswith(skills.PLUGIN + ":"):
            found[n] = {"desc": said.get(n, ""), "src": "Claude Code"}
        elif n in found and not found[n]["desc"] and said.get(n):
            found[n]["desc"] = said[n]
    if names:                             # 会话里报的名单为准：停用的插件、删掉的 Skill 不列
        found = {k: v for k, v in found.items() if k in names or v["src"] in ("项目", "平台")}
    out = [dict(v, name=k) for k, v in found.items()]
    out.sort(key=lambda x: (ORDER.get(x["src"], 9), x["name"].lower()))
    return out
