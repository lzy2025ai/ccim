"""Claude Code 的 UserPromptSubmit 钩子（随插件安装，命令是 ccim-hook）。

用户在桌面端或终端的对话里每发一条消息，先检查这个对话有没有转到过飞书、飞书那边有没有还没问过用户的新内容。
飞书那边是独立的分支，要不要带过来由用户决定：钩子不带内容，只让 Claude 先问一句，用户同意再运行 ccim handback。
每条消息都会跑，所以只做最轻的检查：不加载飞书 SDK，没转出去过的对话读一下登记表就返回。
"""
import json, os, sys

from . import handoff, registry


def _project(cwd):
    """cwd 可能是项目里的子目录，往上找已配对的项目。"""
    data = registry.all_projects()
    p = os.path.realpath(cwd)
    while True:
        if p in data:
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def main():
    try:
        data = json.load(sys.stdin)
        sid, cwd = data.get("session_id"), data.get("cwd") or os.getcwd()
        path = _project(cwd) if sid else None
        if not path:
            return
        ts, pending = handoff.notify_feishu(path, sid)
    except Exception:
        return                        # 钩子出错不能挡住用户发消息
    if not ts:
        return                        # 只有正在回复的一轮时先不问，等它答完
    last = ts[-1]
    recent = " ".join(last["user"].split())[:60]
    context = (f"这个对话之前转到过飞书。飞书那边是一个独立的分支，又聊了 {len(ts)} 轮，还没带到这里"
               f"（最近一轮 {last['when']}：「{recent}」）。用户不一定想把它带过来。"
               "回答用户这条消息之前，先用一句话问用户要不要把飞书那边的内容带过来："
               "要的话运行 ccim handback，读完它的输出再接着聊；不要的话就照常回答，不用再提。")
    if pending:
        context += "另外，飞书那边还有一轮正在回复。"
    print(json.dumps({"systemMessage": f"飞书那边有 {len(ts)} 轮新对话还没带过来",
                      "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
