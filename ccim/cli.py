"""命令入口。

    ccim                       在当前目录前台运行（第一次会先扫码配对）
    ccim start|stop|restart [项目]   后台常驻 / 关停 / 重启
    ccim list                  所有已配对项目和在线状态
    ccim logs [项目] [-f]       查看日志
    ccim config [项目] 键=值     group=owner|all、model=、effort=
    ccim unpair [项目]          解除配对
"""
import argparse, asyncio, logging, os, re, signal, sys

from . import daemon, handoff, registry, runtime
from .channels.feishu import Feishu

CHANNELS = {"feishu": Feishu}
CAPS = ["handoff", "safe-restart"]   # 这个版本的 ccim 进程支持的功能；handoff 据此判断正在运行的是不是旧版本
CONFIG_KEYS = {"group": ("owner", "all"), "model": None, "effort": ("low", "medium", "high", "xhigh", "max"),
               "reaction": None, "runtime": ("dev", "stable")}


# ---------- 运行 ----------

def _logging(mode):
    fmt = "%(asctime)s %(message)s" if mode == "fg" else "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%m-%d %H:%M:%S", stream=sys.stdout, force=True)
    for noisy in ("httpx", "claude_agent_sdk", "Lark"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("Lark").propagate = False   # 飞书 SDK 自己会打一遍，别再转给根日志重复打


async def _serve(path, entry, secret, mode):
    from .bridge import Bridge
    log = logging.getLogger("ccim")
    bridge = Bridge(path, entry)
    channel = CHANNELS[entry["channel"]](path, entry, secret, bridge.handlers())
    bridge.attach(channel)
    registry.write_state(path, pid=os.getpid(), mode=mode, started=registry.now(), online=False, error=None,
                         caps=CAPS, python=sys.executable)
    try:
        await channel.start()
    except Exception as e:
        registry.write_state(path, pid=None, error=str(e), error_at=registry.now())
        raise SystemExit(f"启动失败：{e}")
    registry.write_state(path, online=True, heartbeat=registry.now())
    name = entry.get("bot_name") or "机器人"
    log.info("已连上飞书：在飞书里找「%s」发消息就行%s", name, "。按 Ctrl+C 退出。" if mode == "fg" else "。")
    if entry.get("claim_code"):
        log.info("先在飞书里私聊「%s」，发送 %s 认领这个机器人。", name, entry["claim_code"])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        tick = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except asyncio.TimeoutError:
                tick += 1
                req = handoff.take(path)      # Claude Code 那边要把对话转过来（ccim handoff）
                if req:
                    try:
                        res = await bridge.handoff(req)
                    except Exception as e:
                        log.exception("转接失败")
                        res = {"ok": False, "error": str(e)}
                    handoff.done(path, req, **res)
                if tick % 30 == 0:
                    registry.write_state(path, online=channel.connected(), heartbeat=registry.now())
    finally:
        log.info("正在退出…")
        await bridge.shutdown()
        registry.write_state(path, online=False, pid=None)


def run(path, mode):
    entry = registry.get(path)
    if not entry:
        raise SystemExit(f"{path} 还没配对")
    secret = registry.get_secret(entry["app_id"])
    _logging(mode)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):   # 在 Claude Code 的终端里启动时，别让子进程以为自己是嵌套会话
        os.environ.pop(k, None)
    os.chdir(path)
    asyncio.run(_serve(path, entry, secret, mode))


def pair(path, channel, **kw):
    print(f"给「{os.path.basename(path)}」配对一个飞书机器人。")
    entry = CHANNELS[channel].pair(path, **kw)
    secret = entry.pop("secret")
    registry.set_secret(entry["app_id"], secret)
    entry.update(paired_at=registry.now())
    registry.put(path, entry)
    print(f"\n配对好了：机器人「{entry.get('bot_name') or entry['app_id']}」。")
    return entry


# ---------- 子命令 ----------

def _need(project, single_ok=False):
    path, entry = registry.find(project)
    data = registry.all_projects()
    if not entry and project is None and single_ok and len(data) == 1:   # 只配对过一个项目时，不写也能找到
        path, entry = next(iter(data.items()))
    if not entry:
        if project:
            raise SystemExit(f"找不到「{project}」。用 ccim list 看看已配对的项目。")
        raise SystemExit("当前目录还没配对。先在项目目录里运行 ccim，或者写上项目名、机器人名。")
    return path, entry


def cmd_default(args):
    path = os.path.realpath(os.getcwd())
    entry = registry.get(path) or pair(path, args.channel)
    if entry["channel"] != args.channel:
        raise SystemExit(f"这个项目配对的是 {entry['channel']}。")
    r = daemon.running(path)
    if r:
        where = {"bg": "后台", "always": "后台常驻"}.get(r[1], "另一个终端")
        raise SystemExit(f"它已经在{where}运行了（进程 {r[0]}）。要在这里前台运行，先 ccim stop。")
    run(path, "fg")


def cmd_run(args):
    run(os.path.realpath(args.path), args.mode)


def cmd_start(args):
    path, entry = _need(args.project)
    name = os.path.basename(path)
    r = daemon.running(path)
    if args.always:
        if daemon.is_always(path) and r:
            print(f"「{name}」已经是常驻的了（进程 {r[0]}）。")
            return
        if r and r[1] == "fg":
            raise SystemExit(f"「{name}」正在另一个终端前台运行，先在那里按 Ctrl+C 停掉。")
        if r:
            daemon.stop(path)
        pid = daemon.start_always(path, entry["channel"])
        print(f"「{name}」已常驻（进程 {pid}）：开机登录后自动启动，意外退出会自动重启。ccim stop 关停并取消常驻。")
        return
    if r:
        print(f"「{name}」已经在运行了（进程 {r[0]}）。")
        return
    pid = daemon.start_background(path, entry["channel"])
    print(f"「{name}」已在后台运行（进程 {pid}）。关掉终端也不影响，ccim stop 关停。想开机自启、崩溃自动重启，用 ccim start --always。")


def cmd_stop(args):
    path, _ = _need(args.project)
    name = os.path.basename(path)
    always = daemon.is_always(path)
    if daemon.stop(path):
        print(f"「{name}」已关停" + ("，也不再常驻。" if always else "。"))
    else:
        print(f"「{name}」本来就没在运行。")


def cmd_restart(args):
    path, entry = _need(args.project)
    if args.safe:
        runtime.schedule_restart(path, delay=args.delay)
        print(f"「{os.path.basename(path)}」{args.delay} 秒后重启；新版本起不来会用稳定版顶上，结果发到飞书。")
        return
    if daemon.is_always(path):
        daemon.stop(path)
        pid = daemon.start_always(path, entry["channel"])
        print(f"「{os.path.basename(path)}」已重启，继续常驻（进程 {pid}）。")
        return
    daemon.stop(path)
    pid = daemon.start_background(path, entry["channel"])
    print(f"「{os.path.basename(path)}」已重启，在后台运行（进程 {pid}）。")


def cmd_list(args):
    data = registry.all_projects()
    if not data:
        print("还没有配对过的项目。在项目目录里运行 ccim 开始。")
        return
    rows = [("项目", "渠道", "机器人", "状态", "版本", "路径")]
    for path, e in sorted(data.items()):
        r = daemon.running(path)
        s = registry.read_state(path)
        how = {"bg": "（后台）", "always": "（常驻）"}.get(r[1], "（前台）") if r else ""
        state = ("在线" if s.get("online") else "连接中") + how if r else (
            "离线（等待自动重启）" if daemon.is_always(path) else "离线")
        ver = (runtime.describe(s.get("python")) + ("（顶替中）" if s.get("fallback") else "")) if r else ""
        rows.append((os.path.basename(path), e.get("channel", ""), e.get("bot_name") or e.get("app_id", ""), state,
                     ver, path))
    width = lambda s: sum(2 if ord(c) > 0x2e80 else 1 for c in s)
    cols = [max(width(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i] + " " * (cols[i] - width(r[i])) for i in range(5)) + "  " + r[5])


def cmd_show(args):
    path, e = _need(args.project, single_ok=True)
    s = registry.read_state(path)
    r = daemon.running(path)
    how = {"bg": "后台", "always": "常驻"}.get(r[1], "前台") if r else ""
    if r:
        status = f"{'在线' if s.get('online') else '连接中'}（{how}，进程 {r[0]}，{s.get('started', '')} 启动）"
    else:
        status = "离线（等待自动重启）" if daemon.is_always(path) else "离线"
    default_model, default_effort, m_set, e_set = registry.effective(path, e.get("model"), e.get("effort"))
    follow = "（跟随 Claude Code 设置）"
    rows = [
        ("项目", os.path.basename(path)),
        ("路径", path),
        ("机器人", f"{e.get('bot_name') or e.get('app_id')}（{ {'feishu': '飞书'}.get(e.get('channel'), e.get('channel')) }）"),
        ("状态", status),
        ("版本", (runtime.describe(s.get("python")) + ("（新版本没起来，暂时顶替）" if s.get("fallback") else "")
                  if r else "") + f"　设置：{runtime.which(path, e)[1]}"),
        ("群聊", "所有人 @ 都响应" if e.get("group") == "all" else "只响应主人"),
        ("模型", default_model + ("" if m_set else follow)),
        ("思考深度", default_effort + ("" if e_set else follow)),
        ("收到消息表情", e.get("reaction") or "OnIt（默认）"),
        ("配对时间", e.get("paired_at", "")),
    ]
    if e.get("claim_code"):
        rows.append(("待认领", f"在飞书里私聊机器人发送 {e['claim_code']}"))
    width = lambda t: sum(2 if ord(c) > 0x2e80 else 1 for c in t)
    pad = max(width(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k}{' ' * (pad - width(k))}  {v}")

    chats = s.get("chats") or {}
    if not chats:
        print("\n还没有聊过天。")
        return
    print(f"\n对话（{len(chats)} 个）")
    kinds = {"p2p": "私聊", "group": "群聊"}
    lines = []
    for key, c in sorted(chats.items(), key=lambda kv: kv[1].get("last_active", ""), reverse=True):
        kind = kinds.get(c.get("type"), "聊天") + ("·话题" if "|" in key else "")
        title = f"{kind}「{c['name']}」" if c.get("name") else kind
        model, effort, _, _ = registry.effective(path, c.get("model") or e.get("model"), c.get("effort") or e.get("effort"))
        custom = "（本聊天单独设置）" if c.get("model") or c.get("effort") else ""
        sid = (c.get("session_id") or "")[:8] or "新对话"
        lines.append((title, f"{model} · {effort}{custom}", sid, c.get("last_active") or "—"))
    w = [max(width(l[i]) for l in lines) for i in range(3)]
    for l in lines:
        print("  " + "  ".join(l[i] + " " * (w[i] - width(l[i])) for i in range(3)) + f"  最近 {l[3]}")
    print(f"\n在终端里接着聊：ccim resume {os.path.basename(path)} 对话编号")


def cmd_resume(args):
    """在终端里接着飞书里的某个对话聊。飞书那边还开着这个对话时，终端里另开一个分支，免得两边同时往一个对话里写。"""
    import shutil
    project, prefix = args.project, args.session
    if project and not prefix and re.fullmatch(r"[0-9a-f]{4,}(-[0-9a-f-]*)?", project) and not registry.find(project)[1]:
        project, prefix = None, project               # 只写了对话编号
    path, e = _need(project, single_ok=True)
    chats = registry.read_state(path).get("chats") or {}
    found = [(k, c) for k, c in chats.items() if c.get("session_id") and c["session_id"].startswith(prefix or "")]
    if not found:
        raise SystemExit(f"找不到编号以 {prefix} 开头的对话。用 ccim show 看看有哪些。" if prefix
                         else "这个项目还没有对话。")
    if prefix and len(found) > 1:
        raise SystemExit(f"编号 {prefix} 对应了 {len(found)} 个对话，多写几位。")
    key, chat = max(found, key=lambda kv: kv[1].get("last_active", ""))   # 没写编号就接最近聊过的
    sid = chat["session_id"]
    live = bool(chat.get("live") and daemon.running(path))
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    argv = [claude, "--resume", sid] + (["--fork-session"] if live or args.fork else [])
    if live:
        print("这个对话在飞书那边还开着，终端里接着聊会另开一个分支：之前的内容都在，之后两边各聊各的。")
    print(f"在「{os.path.basename(path)}」里接着对话 {sid[:8]}…")
    os.chdir(path)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(k, None)
    os.execv(argv[0], argv)


def cmd_handoff(args):
    """在 Claude Code 的对话里运行：把这个对话转到飞书私聊接着聊。"""
    sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise SystemExit("要在 Claude Code 的对话里运行（或者写上对话编号）。")
    info = handoff.read(sid, os.getcwd())
    if not info:
        raise SystemExit(f"找不到对话 {sid[:8]} 的记录。")
    path = os.path.realpath(info["cwd"] or os.getcwd())
    entry = registry.get(path) or pair(path, "feishu", show=lambda url: handoff.show_qr_page(path, url),
                                       interactive=False)
    name = os.path.basename(path)
    r = daemon.running(path)
    if r and "handoff" not in (registry.read_state(path).get("caps") or []):
        if r[1] == "fg":
            raise SystemExit(f"「{name}」正在一个终端里前台运行，而且是旧版本。先在那个终端按 Ctrl+C 停掉，再转接。")
        print(f"「{name}」正在运行的 ccim 是旧版本，重启一下…", flush=True)
        if daemon.is_always(path):
            daemon.stop(path)
            daemon.start_always(path, entry["channel"])
        else:
            daemon.stop(path)
            daemon.start_background(path, entry["channel"])
    elif not r:
        print(f"「{name}」的 ccim 没在运行，先在后台启动…", flush=True)
        daemon.start_background(path, entry["channel"])
    effort = os.environ.get("CLAUDE_EFFORT") if not args.session else None
    res = handoff.request(path, {"session_id": sid, "at": info["at"], "title": info["title"],
                                 "last_reply": info["last_reply"], "model": info["model"], "effort": effort})
    if not res.get("ok"):
        raise SystemExit(f"没转过去：{res.get('error')}")
    name = entry.get("bot_name") or "机器人"
    print(f"已转到飞书：「{name}」给你发了一条消息，点开就能接着聊。")
    print("在电脑上打开飞书，或者用手机扫这个码：")
    from .channels.feishu import _print_qr
    link = handoff.open_link(entry)
    _print_qr(link)
    print(link)


def cmd_handback(args):
    """在当初转出去的那个 Claude Code 对话里运行：把飞书那边聊过的内容整理出来，接回这里。"""
    sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise SystemExit("要在当初转到飞书的那个 Claude Code 对话里运行（或者写上对话编号）。")
    info = handoff.read(sid, os.getcwd())
    path = os.path.realpath((info or {}).get("cwd") or os.getcwd())
    entry = registry.get(path)
    chat_id, rec = handoff.find_branch(path, sid) if entry else (None, None)
    if not rec:
        raise SystemExit("这个对话没有转到过飞书。")
    ts, pending = handoff.pull_feishu(path, sid)
    if ts:
        print(f"飞书那边有 {len(ts)} 轮还没带过来，内容如下：\n")
        print(handoff.render(ts))
    elif not pending:
        print("飞书那边没有新内容，直接在这里接着聊就行。")
    if pending:
        print(("\n" if ts else "") + "飞书那边还有一轮正在回复，等它答完再运行一次 ccim handback 带过来。")
    if not ts:
        return
    try:
        ch = CHANNELS[entry["channel"]](path, entry, registry.get_secret(entry["app_id"]), {})
        asyncio.run(ch.send_text(chat_id, "已回到电脑上接着聊了，这边聊过的内容已经带过去。"))
    except Exception as e:
        print(f"\n（没能在飞书里发提醒：{e}）")


def cmd_safe_restart_run(args):
    ok, _ = runtime.safe_restart(os.path.realpath(args.path), delay=args.delay, target=args.target)
    sys.exit(0 if ok else 1)


def cmd_promote(args):
    label, results = runtime.promote()
    for line in results:
        print(line)
    if not results:
        print("没有跑稳定版的项目。")


def cmd_logs(args):
    path, _ = _need(args.project)
    log = daemon.log_path(path)
    if not os.path.exists(log):
        print("还没有日志（只有后台运行才写日志）。")
        return
    os.execvp("tail", ["tail", "-n", str(args.n)] + (["-f"] if args.follow else []) + [log])


def cmd_config(args):
    items = list(args.items)
    project = None
    if items and "=" not in items[0]:
        project = items.pop(0)
    path, entry = _need(project)
    if not items:
        print(f"group={entry.get('group', 'owner')}  model={entry.get('model', '默认')}  effort={entry.get('effort', '默认')}"
              f"  reaction={entry.get('reaction', '默认')}")
        return
    changes = {}
    for it in items:
        k, _, v = it.partition("=")
        if k not in CONFIG_KEYS:
            raise SystemExit(f"不认识 {k}，可以设置：{'、'.join(CONFIG_KEYS)}")
        if CONFIG_KEYS[k] and v not in CONFIG_KEYS[k]:
            raise SystemExit(f"{k} 只能是：{' / '.join(CONFIG_KEYS[k])}")
        changes[k] = v or None
    registry.update(path, **changes)
    print("已保存。" + ("正在运行的要重启才生效：ccim restart " + os.path.basename(path) if daemon.running(path) else ""))


def cmd_unpair(args):
    path, entry = _need(args.project)
    if input(f"解除「{os.path.basename(path)}」和机器人「{entry.get('bot_name') or entry['app_id']}」的配对？(y/N)：").strip().lower() != "y":
        return
    daemon.stop(path)
    registry.del_secret(entry["app_id"])
    registry.remove(path)
    print("已解除。飞书里的那个机器人应用还在，不再需要的话去飞书开放平台删掉。")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ccim", description="把本机 Claude Code 接到飞书。")
    p.add_argument("--feishu", dest="channel", action="store_const", const="feishu", default="feishu",
                   help="用飞书（默认）")
    sub = p.add_subparsers(dest="cmd")
    for name, fn, help_ in [("start", cmd_start, "后台运行"), ("stop", cmd_stop, "关停（常驻的同时取消常驻）"),
                            ("restart", cmd_restart, "重启"), ("unpair", cmd_unpair, "解除配对")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("project", nargs="?", help="项目名或路径，不写就是当前目录")
        if name == "start":
            s.add_argument("--always", action="store_true", help="常驻：开机登录后自动启动，意外退出自动重启")
        if name == "restart":
            s.add_argument("--safe", action="store_true", help="另起进程重启，新版本起不来就用稳定版顶上，结果发到飞书")
            s.add_argument("--delay", type=int, default=15, help="几秒后重启（默认 15，留时间把当前回复发出去）")
        s.set_defaults(fn=fn)
    sub.add_parser("list", help="所有已配对项目").set_defaults(fn=cmd_list)
    s = sub.add_parser("resume", help="在终端里接着飞书里的对话聊（不写编号就接最近聊过的）")
    s.add_argument("project", nargs="?", help="项目名、机器人名或路径；也可以直接写对话编号")
    s.add_argument("session", nargs="?", help="对话编号，ccim show 里那 8 位就行")
    s.add_argument("--fork", action="store_true", help="另开一个分支，不动飞书里的对话")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("handoff", help="在 Claude Code 的对话里运行：把这个对话转到飞书接着聊")
    s.add_argument("session", nargs="?", help="对话编号，不写就是当前所在的对话")
    s.set_defaults(fn=cmd_handoff)
    s = sub.add_parser("handback", help="在当初转出去的那个 Claude Code 对话里运行：把飞书那边聊过的内容接回来")
    s.add_argument("session", nargs="?", help="对话编号，不写就是当前所在的对话")
    s.set_defaults(fn=cmd_handback)
    s = sub.add_parser("show", help="查看一个项目的详情：状态、模型、思考深度、各个对话")
    s.add_argument("project", nargs="?", help="项目名、机器人名或路径，不写就是当前目录")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("logs", help="查看后台日志")
    s.add_argument("project", nargs="?")
    s.add_argument("-f", "--follow", action="store_true", help="持续输出")
    s.add_argument("-n", type=int, default=60)
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("config", help="项目设置：group=owner|all model=名字 effort=等级")
    s.add_argument("items", nargs="*")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("promote", help="验收通过后：把当前代码装成稳定版，逐个升级跑稳定版的项目")
    s.set_defaults(fn=cmd_promote)
    s = sub.add_parser("safe-restart-run")    # 内部用：安全重启的独立进程
    s.add_argument("path")
    s.add_argument("--delay", type=float, default=0)
    s.add_argument("--target", choices=("dev", "stable"))
    s.set_defaults(fn=cmd_safe_restart_run)
    s = sub.add_parser("run")                 # 内部用：后台进程的入口
    s.add_argument("path")
    s.add_argument("--channel", default="feishu")
    s.add_argument("--mode", default="fg")
    s.set_defaults(fn=cmd_run)
    args = p.parse_args(argv)
    try:
        (getattr(args, "fn", None) or cmd_default)(args)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
