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
from .channels import NAMES, load, send_once

CAPS = ["handoff", "safe-restart"]   # 这个版本的 ccim 进程支持的功能；handoff 据此判断正在运行的是不是旧版本
CONFIG_KEYS = {"group": ("owner", "all"), "model": None, "effort": ("low", "medium", "high", "xhigh", "max"),
               "reaction": None, "runtime": ("dev", "stable"), "agent": ("claude", "codex")}


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
    if entry.get("kind") == "weixin":
        from .weixin_hub import Hub
        bridge = Hub(path, entry)
    else:
        bridge = Bridge(path, entry)
    channel = load(entry["channel"])(path, entry, secret, bridge.handlers())
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
    end = "。按 Ctrl+C 退出。" if mode == "fg" else "。"
    if channel.cards:
        log.info("已连上%s：在%s里找「%s」发消息就行%s", channel.label, channel.label, name, end)
    else:
        log.info("已连上%s：在%s里给它发消息就行%s", channel.label, channel.label, end)
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
                    registry.write_state(path, online=channel.connected(), heartbeat=registry.now(),
                                         error=channel.error, **({"error_at": registry.now()} if channel.error else {}))
    finally:
        log.info("正在退出…")
        await bridge.shutdown()
        await channel.stop()
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
    print(f"把「{os.path.basename(path)}」接入{NAMES[channel]}。")
    entry = load(channel).pair(path, **kw)
    secret = entry.pop("secret")
    registry.set_secret(entry["app_id"], secret)
    entry.update(paired_at=registry.now())
    registry.put(path, entry)
    print(f"\n配对好了：机器人「{entry.get('bot_name') or entry['app_id']}」。")
    return entry


# ---------- 子命令 ----------

def _weixin_only(project):
    """只接了微信的项目（登记在微信号下面，没有自己的登记）：返回路径，不是返回 None。"""
    from . import weixin_hub
    all_ = {p for _, e in weixin_hub.hubs() for p in (e.get("projects") or {})}
    p = os.path.realpath(os.path.expanduser(project or os.getcwd()))
    if p in all_:
        return p
    hits = [x for x in all_ if os.path.basename(x) == project]
    return hits[0] if project and len(hits) == 1 else None


def _need(project, single_ok=False, weixin_ok=False):
    """weixin_ok：只接了微信的项目也行，这时返回的条目是空的。"""
    path, entry = registry.find(project)
    data = registry.all_projects()
    if not entry and project is None and single_ok and len(data) == 1:   # 只配对过一个项目时，不写也能找到
        path, entry = next(iter(data.items()))
    if not entry and _weixin_only(project):
        from . import weixin_hub
        path = _weixin_only(project)
        if weixin_ok:
            return path, {}
        names = "、".join(e.get("bot_name") for _, e in weixin_hub.hubs_of(path))
        raise SystemExit(f"「{os.path.basename(path)}」只接了微信，由微信「{names}」负责：开关、重启、看日志用 ccim start / stop / "
                         f"restart / logs 加上微信号的名字。")
    if not entry:
        if project:
            raise SystemExit(f"找不到「{project}」。用 ccim list 看看已配对的项目。")
        raise SystemExit("当前目录还没配对。先在项目目录里运行 ccim，或者写上项目名、机器人名。")
    return path, entry


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def _apply_agent(path, agent):
    """--agent：换这个项目用的助手。返回是否换了。两边的对话分开记，切回来还能接着原来的聊。"""
    entry = registry.get(path)
    if not agent or (entry.get("agent") or "claude") == agent:
        return False
    registry.update(path, agent=None if agent == "claude" else agent)
    print(f"「{os.path.basename(path)}」改用 {AGENT_NAMES[agent]}。")
    desc = runtime.which(path, registry.get(path))[1]
    if agent == "codex" and desc.startswith("稳定版 ") and desc.split()[1] < "0.8":
        print(f"它跑的{desc}还不支持 Codex，先改用开发版运行。")
        registry.update(path, runtime="dev")
    return True


def add_weixin(path, name, agent):
    """ccim --weixin：扫码，扫的哪个微信就加到哪个（已经登录过的不会重复登录）。
    ccim --weixin 名字：不扫码，直接加到这个已登录的微信号；还没有这个名字就扫码登录一个新的。"""
    from . import weixin_hub
    hub, e = weixin_hub.find(name) if name else (None, None)
    if not hub:
        if name:
            print(f"还没有叫「{name}」的微信号，扫码登录一个。")
        hub, e, existed, changed = weixin_hub.login(name)
        if existed and changed and daemon.running(hub):   # 重新扫码后旧的登录凭证马上失效，后台进程要换上新的
            print("重启一下这个微信，用上新的登录…", flush=True)
            daemon.stop(hub)
        if name and existed and e.get("bot_name") != name:   # 想登录新的号，却拿已经登录过的微信扫了
            if e.get("projects"):
                weixin_hub.ensure_running(hub)
            raise SystemExit(f"扫码的是已经登录过的微信「{e['bot_name']}」，不是新的微信，「{os.path.basename(path)}」没有加进去。\n"
                             f"要登录「{name}」，用另一个微信扫码：ccim --weixin {name}")
    else:
        changed = False
    pname = os.path.basename(path)
    already = path in (e.get("projects") or {})
    weixin_hub.add_project(hub, path, agent=None if agent == "claude" else agent)
    switched = False
    if changed and not already:
        # 重新扫过码，发不了提醒：直接切到新项目，用户在微信里发下一条消息时先告诉他
        old = registry.read_state(hub).get("current")
        if old and old != path and os.path.realpath(old) in (registry.get(hub).get("projects") or {}):
            back = os.path.basename(old)
            registry.write_state(hub, current=path, notice=f"已切到新加的项目「{pname}」，要回到「{back}」发 /项目 {back}")
            switched = True
    weixin_hub.ensure_running(hub)
    n = len(registry.get(hub).get("projects") or {})
    print(f"「{pname}」{'本来就在' if already else '已加到'}微信「{e['bot_name']}」里。" +
          ("微信里已经切到它，直接发消息就行。" if switched else "直接在微信里给它发消息就行。" if n == 1
           else f"在微信里发 /项目 {pname} 切过去。"))
    # 刚重新扫过码的话不发：换了登录后，用户在微信里发消息之前，机器人主动发的消息微信会丢掉
    if n > 1 and not already and not changed:
        try:
            asyncio.run(send_once(hub, registry.get(hub), f"新加了项目「{pname}」，发 /项目 {pname} 切过去。"))
        except BaseException as err:
            print(f"（没能在微信里提醒：{err}）")


def _check_dir(path, yes):
    """接入前看看这个目录合不合适：根目录不行；主目录要确认（--yes 跳过；不在终端里又没加 --yes 就不接）。"""
    if path == "/":
        raise SystemExit("这里是根目录，先 cd 到项目目录再运行。")
    if path != os.path.realpath(os.path.expanduser("~")) or registry.get(path) or _weixin_only(path) or yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("这里是用户主目录，Claude 能读写你所有的文件。确定要接入的话加上 --yes；接某个项目的话先 cd 到项目目录。")
    if input("这里是用户主目录，Claude 能读写你所有的文件。确定要接入吗？(y/N)：").strip().lower() != "y":
        raise SystemExit("没有接入。要接某个项目的话，先 cd 到项目目录再运行。")


def cmd_default(args):
    path = os.path.realpath(os.getcwd())
    _check_dir(path, args.yes)
    if args.weixin is not None:
        return add_weixin(path, args.weixin or None, args.agent)
    entry = registry.get(path) or pair(path, args.channel or "feishu")
    _apply_agent(path, args.agent)
    entry = registry.get(path)
    r = daemon.running(path)
    if r:
        where = {"bg": "后台", "always": "后台常驻"}.get(r[1], "另一个终端")
        raise SystemExit(f"它已经在{where}运行了（进程 {r[0]}）。要在这里前台运行，先 ccim stop。")
    run(path, "fg")


def cmd_run(args):
    run(os.path.realpath(args.path), args.mode)


def cmd_start(args):
    if args.project is None and not registry.find(None)[1] and not _weixin_only(None):
        path = os.path.realpath(os.getcwd())     # 还没配对：先扫码配对，配好直接后台运行
        _check_dir(path, args.yes)
        pair(path, "feishu")
    path, entry = _need(args.project)
    name = os.path.basename(path)
    r = daemon.running(path)
    if _apply_agent(path, args.agent) and r:      # 换了助手：正在运行的要重启才生效
        if r[1] == "fg":
            raise SystemExit(f"「{name}」正在另一个终端前台运行，先在那里按 Ctrl+C 停掉，再运行一次。")
        daemon.stop(path)
        r = None
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
    _apply_agent(path, args.agent)
    if args.safe:
        runtime.schedule_restart(path, delay=args.delay)
        print(f"「{os.path.basename(path)}」{args.delay} 秒后重启；新版本起不来会用稳定版顶上，"
              f"结果发到{NAMES[entry['channel']]}。")
        return
    if daemon.is_always(path):
        daemon.stop(path)
        pid = daemon.start_always(path, entry["channel"])
        print(f"「{os.path.basename(path)}」已重启，继续常驻（进程 {pid}）。")
        return
    daemon.stop(path)
    pid = daemon.start_background(path, entry["channel"])
    print(f"「{os.path.basename(path)}」已重启，在后台运行（进程 {pid}）。")


def _state_text(path):
    r = daemon.running(path)
    s = registry.read_state(path)
    if r and s.get("error"):
        return "出错：" + s["error"], s, r
    how = {"bg": "（后台）", "always": "（常驻）"}.get(r[1], "（前台）") if r else ""
    state = ("在线" if s.get("online") else "连接中") + how if r else (
        "离线（等待自动重启）" if daemon.is_always(path) else "离线")
    return state, s, r


def cmd_list(args):
    data = registry.all_projects()
    if not data:
        print("还没有配对过的项目。在项目目录里运行 ccim 开始。")
        return
    rows = [("项目", "渠道", "机器人", "状态", "版本", "路径")]
    for path, e in sorted(data.items(), key=lambda kv: (kv[1].get("kind") == "weixin", kv[0])):
        state, s, r = _state_text(path)
        ver = (runtime.describe(s.get("python")) + ("（顶替中）" if s.get("fallback") else "")) if r else ""
        if e.get("kind") == "weixin":
            names = "、".join(os.path.basename(p) for p in e.get("projects") or {}) or "（还没有项目）"
            rows.append((names, "微信", e.get("bot_name", ""), state, ver, ""))
            continue
        rows.append((os.path.basename(path), NAMES.get(e.get("channel"), e.get("channel", "")),
                     (e.get("bot_name") or e.get("app_id", "")) + ("（Codex）" if e.get("agent") == "codex" else ""), state,
                     ver, path))
    width = lambda s: sum(2 if ord(c) > 0x2e80 else 1 for c in s)
    cols = [max(width(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print(("  ".join(r[i] + " " * (cols[i] - width(r[i])) for i in range(5)) + "  " + r[5]).rstrip())


def _codex_effective(path, model=None, effort=None):
    from .codex import defaults
    m, e = defaults(path)
    return model or m or "默认", effort or e or "默认", bool(model), bool(effort)


def _print_rows(rows):
    width = lambda t: sum(2 if ord(c) > 0x2e80 else 1 for c in t)
    pad = max(width(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k}{' ' * (pad - width(k))}  {v}")


def _show_hub(path, e):
    state, s, r = _state_text(path)
    cur = s.get("current")
    projects = list(e.get("projects") or {})
    _print_rows([
        ("微信号", e.get("bot_name", "")),
        ("状态", state + (f"，进程 {r[0]}，{s.get('started', '')} 启动" if r else "")),
        ("版本", (runtime.describe(s.get("python")) if r else "") + f"　设置：{runtime.which(path, e)[1]}"),
        ("项目", "、".join(os.path.basename(p) + ("（当前）" if p == cur else "") for p in projects) or "还没有，到项目目录里运行 ccim --weixin"),
        ("登录时间", e.get("paired_at", "")),
    ])


def cmd_show(args):
    from . import weixin_hub
    path, e = _need(args.project, single_ok=True, weixin_ok=True)
    if e.get("kind") == "weixin":
        return _show_hub(path, e)
    hubs = weixin_hub.hubs_of(path)
    settings = {k: v for _, h in hubs[:1] for k, v in ((h.get("projects") or {}).get(path) or {}).items()} if not e else e
    codex = settings.get("agent") == "codex"
    eff = _codex_effective if codex else registry.effective
    default_model, default_effort, m_set, e_set = eff(path, settings.get("model"), settings.get("effort"))
    follow = "（跟随 Codex 设置）" if codex else "（跟随 Claude Code 设置）"
    rows = [("项目", os.path.basename(path)), ("路径", path)]
    if e:
        state, s, r = _state_text(path)
        rows += [
            ("机器人", f"{e.get('bot_name') or e.get('app_id')}（{NAMES.get(e.get('channel'), e.get('channel'))}）"),
            ("状态", state + (f"，进程 {r[0]}，{s.get('started', '')} 启动" if r else "")),
            ("版本", (runtime.describe(s.get("python")) + ("（新版本没起来，暂时顶替）" if s.get("fallback") else "")
                      if r else "") + f"　设置：{runtime.which(path, e)[1]}"),
            ("群聊", "所有人 @ 都响应" if e.get("group") == "all" else "只响应主人"),
        ]
    if hubs:
        rows.append(("微信", "、".join(f"「{h.get('bot_name')}」（{_state_text(hp)[0]}）" for hp, h in hubs)))
    rows += [
        ("助手", "Codex" if codex else "Claude Code"),
        ("模型", default_model + ("" if m_set else follow)),
        ("思考深度", default_effort + ("" if e_set else follow)),
    ]
    if e:
        rows += [("收到消息表情", e.get("reaction") or "OnIt（默认）"), ("配对时间", e.get("paired_at", ""))]
    if e.get("claim_code"):
        rows.append(("待认领", f"在{NAMES[e['channel']]}里私聊机器人发送 {e['claim_code']}"))
    _print_rows(rows)

    chats = registry.all_chats(path)
    if not chats:
        print("\n还没有聊过天。")
        return
    print(f"\n对话（{len(chats)} 个）")
    width = lambda t: sum(2 if ord(c) > 0x2e80 else 1 for c in t)
    kinds = {"p2p": "私聊", "group": "群聊"}
    lines = []
    for where, key, c in sorted(chats, key=lambda x: x[2].get("last_active", ""), reverse=True):
        kind = where + kinds.get(c.get("type"), "聊天") + ("·话题" if "|" in key else "")
        title = f"{kind}「{c['name']}」" if c.get("name") else kind
        model, effort, _, _ = eff(path, c.get("model") or settings.get("model"), c.get("effort") or settings.get("effort"))
        custom = "（本聊天单独设置）" if c.get("model") or c.get("effort") else ""
        sid = (c.get("codex_session_id" if codex else "session_id") or "")[:8] or "新对话"
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
    path, e = _need(project, single_ok=True, weixin_ok=True)
    if e.get("agent") == "codex":
        raise SystemExit("这个项目用的是 Codex，在终端里用 codex resume 接着聊。")
    found = [(w, c) for w, _, c in registry.all_chats(path)
             if c.get("session_id") and c["session_id"].startswith(prefix or "")]
    if not found:
        raise SystemExit(f"找不到编号以 {prefix} 开头的对话。用 ccim show 看看有哪些。" if prefix
                         else "这个项目还没有对话。")
    if prefix and len(found) > 1:
        raise SystemExit(f"编号 {prefix} 对应了 {len(found)} 个对话，多写几位。")
    where, chat = max(found, key=lambda kv: kv[1].get("last_active", ""))   # 没写编号就接最近聊过的
    sid = chat["session_id"]
    live = bool(chat.get("live"))
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    argv = [claude, "--resume", sid] + (["--fork-session"] if live or args.fork else [])
    if live:
        print(f"这个对话在{where}那边还开着，终端里接着聊会另开一个分支：之前的内容都在，之后两边各聊各的。")
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
    if not registry.get(path) and _weixin_only(path):
        raise SystemExit("这个项目只接了微信，还不能从这里转过去。可以在微信里发 /resume 接上这个对话。")
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
    label = NAMES[entry["channel"]]
    print(f"已转到{label}：「{name}」给你发了一条消息，点开就能接着聊。")
    if entry["channel"] != "feishu":
        return
    print("在电脑上打开飞书，或者用手机扫这个码：")
    from .channels.feishu import _print_qr
    link = handoff.open_link(entry)
    _print_qr(link)
    print(link)


def cmd_handback(args):
    """在当初转出去的那个 Claude Code 对话里运行：把飞书那边聊过的内容整理出来，接回这里。"""
    sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise SystemExit("要在当初转出去的那个 Claude Code 对话里运行（或者写上对话编号）。")
    info = handoff.read(sid, os.getcwd())
    path = os.path.realpath((info or {}).get("cwd") or os.getcwd())
    entry = registry.get(path)
    chat_id, rec = handoff.find_branch(path, sid) if entry else (None, None)
    if not rec:
        raise SystemExit("这个对话没有转到过飞书或微信。")
    label = NAMES[entry["channel"]]
    ts, pending = handoff.pull_feishu(path, sid)
    if ts:
        print(f"{label}那边有 {len(ts)} 轮还没带过来，内容如下：\n")
        print(handoff.render(ts))
    elif not pending:
        print(f"{label}那边没有新内容，直接在这里接着聊就行。")
    if pending:
        print(("\n" if ts else "") + f"{label}那边还有一轮正在回复，等它答完再运行一次 ccim handback 带过来。")
    if not ts:
        return
    try:
        asyncio.run(send_once(path, entry, "已回到电脑上接着聊了，这边聊过的内容已经带过去。", chat_id))
    except Exception as e:
        print(f"\n（没能在{label}里发提醒：{e}）")



def cmd_safe_restart_run(args):
    ok, _ = runtime.safe_restart(os.path.realpath(args.path), delay=args.delay, target=args.target)
    sys.exit(0 if ok else 1)


def cmd_promote(args):
    label, results = runtime.promote()
    for line in results:
        print(line)
    if not results:
        print("没有跑稳定版的项目。")


def cmd_weixin(args):
    from . import weixin_hub
    act, name = args.action, args.name
    if act == "login":
        hub, e, existed, _ = weixin_hub.login(name)
        if daemon.running(hub):               # 重新登录换了 token，要重启才用上
            daemon.stop(hub)
        if e.get("projects"):
            weixin_hub.ensure_running(hub)
        if name and existed and e.get("bot_name") != name:
            raise SystemExit(f"扫码的是已经登录过的微信「{e['bot_name']}」（顺便更新了它的登录），不是新的微信。"
                             f"要登录「{name}」，用另一个微信扫码。")
        if e.get("projects"):
            print(f"微信「{e['bot_name']}」已在后台常驻。")
        else:
            print(f"到项目目录里运行 ccim --weixin {e['bot_name']}，把项目加进来。")
        return
    if act == "remove":
        path = _weixin_only(name) or os.path.realpath(os.path.expanduser(name or os.getcwd()))
        hubs = [(h, e) for h, e in weixin_hub.hubs_of(path) if not args.account or e.get("bot_name") == args.account]
        if not hubs:
            raise SystemExit(f"「{os.path.basename(path)}」不在{'微信「' + args.account + '」' if args.account else '任何微信号'}里。")
        for h, e in hubs:
            weixin_hub.remove_project(h, path)
            print(f"已把「{os.path.basename(path)}」移出微信「{e['bot_name']}」。")
        return
    if act == "rename":
        if not name or not args.new:
            raise SystemExit("用法：ccim weixin rename 现在的名字 新名字")
        weixin_hub.rename(name, args.new)
        print(f"已改名：「{name}」→「{args.new}」。")
        return
    if act == "logout":
        hub, e = weixin_hub.find(name)
        if not hub:
            raise SystemExit(f"没有叫「{name}」的微信号。" if name else "还没有登录微信。")
        if input(f"退出微信「{e['bot_name']}」？它管的项目在微信里就用不了了。(y/N)：").strip().lower() != "y":
            return
        daemon.stop(hub)
        registry.del_secret(e["app_id"])
        registry.remove(hub)
        print("已退出。")
        return
    hs = weixin_hub.hubs()
    if not hs:
        print("还没有登录微信。到项目目录里运行 ccim --weixin，扫码登录并把项目加进来。")
        return
    for hub, e in hs:
        cur = registry.read_state(hub).get("current")
        print(f"微信「{e.get('bot_name')}」　{_state_text(hub)[0]}")
        for p in e.get("projects") or {}:
            print(f"  {os.path.basename(p)}{'（当前）' if p == cur else ''}　{p}")
        if not e.get("projects"):
            print("  还没有项目")


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
    if entry.get("kind") == "weixin":
        raise SystemExit("微信号没有这些设置。")
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
    if entry.get("kind") == "weixin":
        raise SystemExit(f"退出微信号用 ccim weixin logout {entry.get('bot_name')}。")
    if input(f"解除「{os.path.basename(path)}」和机器人「{entry.get('bot_name') or entry['app_id']}」的配对？(y/N)：").strip().lower() != "y":
        return
    daemon.stop(path)
    registry.del_secret(entry["app_id"])
    registry.remove(path)
    print("已解除。" + ("飞书里的那个机器人应用还在，不再需要的话去飞书开放平台删掉。" if entry["channel"] == "feishu" else ""))


def parser():
    p = argparse.ArgumentParser(prog="ccim", description="把本机 Claude Code 接到飞书或微信。")
    p.add_argument("--feishu", dest="channel", action="store_const", const="feishu", help="用飞书（默认）")
    p.add_argument("--weixin", nargs="?", const="", metavar="微信号",
                   help="把当前目录加进微信（登录了好几个微信号时写上名字）")
    p.add_argument("--agent", choices=("claude", "codex"), help="用哪个助手：claude（默认）或 codex")
    p.add_argument("--yes", "-y", action="store_true", help="不问确认（比如在用户主目录接入）")
    sub = p.add_subparsers(dest="cmd")
    for name, fn, help_ in [("start", cmd_start, "后台运行（没配对过就先扫码配对）"), ("stop", cmd_stop, "关停（常驻的同时取消常驻）"),
                            ("restart", cmd_restart, "重启"), ("unpair", cmd_unpair, "解除配对")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("project", nargs="?", help="项目名或路径，不写就是当前目录")
        if name == "start":
            s.add_argument("--always", action="store_true", help="常驻：开机登录后自动启动，意外退出自动重启")
        if name in ("start", "restart"):
            s.add_argument("--agent", choices=("claude", "codex"), help="换用哪个助手：claude 或 codex")
        if name == "start":
            s.add_argument("--yes", "-y", action="store_true", default=argparse.SUPPRESS, help="不问确认（比如在用户主目录接入）")
        if name == "restart":
            s.add_argument("--safe", action="store_true", help="另起进程重启，新版本起不来就用稳定版顶上，结果私聊告诉你")
            s.add_argument("--delay", type=int, default=15, help="几秒后重启（默认 15，留时间把当前回复发出去）")
        s.set_defaults(fn=fn)
    sub.add_parser("list", help="所有已配对项目").set_defaults(fn=cmd_list)
    s = sub.add_parser("resume", help="在终端里接着飞书 / 微信里的对话聊（不写编号就接最近聊过的）")
    s.add_argument("project", nargs="?", help="项目名、机器人名或路径；也可以直接写对话编号")
    s.add_argument("session", nargs="?", help="对话编号，ccim show 里那 8 位就行")
    s.add_argument("--fork", action="store_true", help="另开一个分支，不动飞书 / 微信里的对话")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("handoff", help="在 Claude Code 的对话里运行：把这个对话转到飞书 / 微信接着聊")
    s.add_argument("session", nargs="?", help="对话编号，不写就是当前所在的对话")
    s.set_defaults(fn=cmd_handoff)
    s = sub.add_parser("handback", help="在当初转出去的那个 Claude Code 对话里运行：把飞书 / 微信那边聊过的内容接回来")
    s.add_argument("session", nargs="?", help="对话编号，不写就是当前所在的对话")
    s.set_defaults(fn=cmd_handback)
    s = sub.add_parser("show", help="查看一个项目的详情：状态、模型、思考深度、各个对话")
    s.add_argument("project", nargs="?", help="项目名、机器人名或路径，不写就是当前目录")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("weixin", help="微信号：不写参数是查看；login [名字] 登录；rename 名字 新名字 改名；"
                                      "remove [项目] 移出项目；logout 名字 退出")
    s.add_argument("action", nargs="?", choices=("login", "rename", "remove", "logout"))
    s.add_argument("name", nargs="?", help="login / rename / logout 写微信号的名字，remove 写项目")
    s.add_argument("new", nargs="?", help="rename 的新名字")
    s.add_argument("--from", dest="account", help="remove 时只从这个微信号移出")
    s.set_defaults(fn=cmd_weixin)
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
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if getattr(args, "fn", None) not in (cmd_run, cmd_safe_restart_run):
        from . import weixin_hub
        weixin_hub.migrate()
    try:
        (getattr(args, "fn", None) or cmd_default)(args)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
