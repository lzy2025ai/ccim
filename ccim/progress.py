"""把 Claude 的工具调用写成一行人话，用在「工作中」卡片上。"""
import os


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def describe_tool(name, inp):
    """返回 None 表示不值得显示。"""
    inp = inp or {}
    base = lambda p: os.path.basename(str(p or "").rstrip("/"))
    if name == "Read":
        return f"读取 {base(inp.get('file_path'))}"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"修改 {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Bash":
        return "运行：" + short(inp.get("description") or inp.get("command"), 80)
    if name == "Skill":
        return f"使用 skill：{inp.get('skill', '')}"
    if name in ("Glob", "Grep"):
        return f"查找 {short(inp.get('pattern'), 50)}"
    if name in ("Agent", "Task"):
        return "派出子任务：" + short(inp.get("description") or inp.get("prompt"), 50)
    if name == "WebFetch":
        return "查资料：" + short(inp.get("url"), 60)
    if name == "WebSearch":
        return "搜索：" + short(inp.get("query"), 50)
    if name in ("TodoWrite", "ToolSearch", "BashOutput"):
        return None
    if name.startswith("mcp__ccim__send_file"):
        return f"发送文件 {base(inp.get('path'))}"
    if name.startswith("mcp__"):
        return "调用工具 " + name.split("__")[-1]
    return name


def describe_request(name, inp):
    """审批卡片上「要做什么」那一段：比工作中卡片写得完整一些。"""
    inp = inp or {}
    if name == "Bash":
        desc = inp.get("description")
        return (f"{desc}\n" if desc else "") + f"```\n{short(inp.get('command'), 600)}\n```"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"修改文件 `{inp.get('file_path') or inp.get('notebook_path')}`"
    if name == "WebFetch":
        return f"访问网页 {inp.get('url')}"
    return f"{name}\n```\n{short(inp, 600)}\n```"


def clock(sec):
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"
