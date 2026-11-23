"""在用户的另一台电脑上执行命令、取回文件（#546）。

Agent 调 run_on_machine：本机平台把请求经 peers.forward 交给那台电脑的 /api/remote/exec，那台执行完把输出和要取的文件
原样传回，文件存到本机 ~/.ccim/tmp/remote/ 下，Agent 用本机路径打开。

- 那台电脑要自己打开开关（remote.json 的 allow，默认关），在网页「设置 → 电脑 → 那台电脑」里开。
- 只接受别的电脑转来的（X-From-Peer）、主人的（没有 X-Account）；访客的请求本来就过不了 share 的白名单。
- 命令用登录 shell 跑（和终端里一样能找到用户装的命令行工具），不能交互（标准输入是空的）。
- 每次执行记一条到 remote-log.jsonl（哪台电脑、哪个 Agent、命令、退出码、多久），电脑页显示最近的。
- 工具不进免确认列表：和本机的 Bash 一样由权限判断决定要不要用户点头。
"""
import base64, json, logging, os, secrets, subprocess, threading, time

from . import registry

log = logging.getLogger("ccim.remote")

TIMEOUT = 120                                 # 默认最多跑这么久
MAX_TIMEOUT = 600                             # 经总控转交最长等 660 秒（peers.CALL_TIMEOUT），留点余量
OUTPUT_LIMIT = 50000                          # 标准输出、错误各留这么多（留尾巴：出错信息多在最后）
FILE_LIMIT = 20 * 1024 * 1024                 # 一个文件最大
FILES_LIMIT = 40 * 1024 * 1024                # 一次取回的文件合计最大
LOG_KEEP = 200
SHOW = 20                                     # 电脑页显示最近几条
_lock = threading.Lock()


class Problem(Exception):
    pass


def _conf():
    return os.path.join(registry.HOME, "remote.json")


def _log():
    return os.path.join(registry.HOME, "remote-log.jsonl")


def allowed():
    try:
        with open(_conf()) as f:
            return bool(json.load(f).get("allow"))
    except (OSError, ValueError):
        return False


def set_allow(on):
    p = _conf()
    with open(p + ".tmp", "w") as f:
        json.dump({"allow": bool(on), "at": time.time()}, f)
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)
    _record({"kind": "allow", "on": bool(on)})
    return info()


def recent(n=SHOW):
    try:
        with open(_log()) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def info():
    return {"allow": allowed(), "log": recent()}


def _record(rec):
    rec = dict(rec, at=time.time())
    p = _log()
    with _lock:
        try:
            with open(p) as f:
                lines = f.readlines()
        except OSError:
            lines = []
        lines = (lines + [json.dumps(rec, ensure_ascii=False) + "\n"])[-LOG_KEEP:]
        with open(p + ".tmp", "w") as f:
            f.writelines(lines)
        os.chmod(p + ".tmp", 0o600)
        os.replace(p + ".tmp", p)


def _tail(b):
    s = b.decode(errors="replace")
    return s if len(s) <= OUTPUT_LIMIT else f"…（前面 {len(s) - OUTPUT_LIMIT} 个字没显示）\n" + s[-OUTPUT_LIMIT:]


def _shell():
    s = os.environ.get("SHELL") or ""
    return s if os.path.basename(s) in ("zsh", "bash") and os.path.exists(s) else "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh"


def _files(paths, cwd):
    out, total = [], 0
    for raw in paths or []:
        p = os.path.realpath(os.path.join(cwd, os.path.expanduser(str(raw))))
        item = {"path": p, "name": os.path.basename(p)}
        if not os.path.isfile(p):
            out.append(dict(item, error="文件不存在"))
            continue
        size = os.path.getsize(p)
        if size > FILE_LIMIT:
            out.append(dict(item, error=f"文件太大（{size // 1024 // 1024} MB），一个最多 {FILE_LIMIT // 1024 // 1024} MB"))
            continue
        if total + size > FILES_LIMIT:
            out.append(dict(item, error=f"这次取回的文件合计超过 {FILES_LIMIT // 1024 // 1024} MB，这个没取"))
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError as e:
            out.append(dict(item, error=f"读不了：{e.strerror or e}"))
            continue
        total += size
        out.append(dict(item, size=size, data=base64.b64encode(data).decode()))
    return out


def execute(body, headers):
    """那台电脑上：执行别的电脑转来的命令。"""
    if not headers.get("X-From-Peer") or headers.get("X-Account"):
        raise Problem("只接受你自己其他电脑上的 Agent。")
    if not allowed():
        raise Problem("这台电脑没有打开「允许别的电脑执行命令」。请用户在网页「设置 → 电脑」里点开这台电脑，打开这个开关。")
    command = str(body.get("command") or "").strip()
    if not command:
        raise Problem("command 是空的。")
    cwd = os.path.expanduser(str(body.get("cwd") or "~"))
    if not os.path.isdir(cwd):
        raise Problem(f"这台电脑上没有这个文件夹：{cwd}")
    try:
        timeout = max(1, min(int(body.get("timeout") or TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = TIMEOUT
    who = {"from": str(body.get("from") or "")[:80], "by": str(body.get("by") or "")[:80]}
    start = time.time()
    p = subprocess.Popen([_shell(), "-lc", command], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    killed = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        killed = True
        try:
            os.killpg(p.pid, 9)
        except OSError:
            p.kill()
        out, err = p.communicate()
    took = round(time.time() - start, 1)
    _record(dict(who, kind="exec", command=command[:2000], cwd=cwd, code=p.returncode, killed=killed, took=took))
    return {"code": p.returncode, "killed": killed, "timeout": timeout, "took": took, "cwd": cwd,
            "stdout": _tail(out), "stderr": _tail(err), "files": _files(body.get("files"), cwd)}


# ----- 发起的一方（Agent 所在的电脑） -----

def targets(peers):
    """能选的电脑：自己的（不算别人共享过来的）、不是这台。"""
    return [m for m in peers.machines(own=True) if not m.get("me") and not m.get("shared")]


def find(peers, name):
    name = str(name or "").strip()
    ms = targets(peers)
    hit = [m for m in ms if m["name"] == name or m["id"] == name] or [m for m in ms if name and name.lower() in m["name"].lower()]
    if len(hit) == 1:
        return hit[0], None
    names = "、".join(f"「{m['name']}」" for m in ms) or "（没有，用户只接了这一台电脑）"
    if any(m["name"] == name for m in peers.machines(own=True) if m.get("me")):
        return None, "这就是你所在的电脑，直接用 Bash 执行。"
    return None, (f"「{name}」对应了好几台，写完整的名字。" if hit else f"没有叫「{name}」的电脑。") + f"能用的：{names}"


def save_files(files):
    """把取回的文件存到本机，返回 [(那台电脑上的路径, 本机路径或 None, 出错原因)]。"""
    if not files:
        return []
    d = os.path.join(registry.HOME, "tmp", "remote", time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    os.makedirs(d, mode=0o700, exist_ok=True)
    out, used = [], set()
    for f in files:
        if f.get("error"):
            out.append((f.get("path"), None, f["error"]))
            continue
        name = os.path.basename(f.get("name") or "file") or "file"
        base, ext = os.path.splitext(name)
        n = 1
        while name in used:
            n += 1
            name = f"{base}-{n}{ext}"
        used.add(name)
        local = os.path.join(d, name)
        with open(local, "wb") as fp:
            fp.write(base64.b64decode(f.get("data") or ""))
        out.append((f.get("path"), local, None))
    return out


def report(machine, r):
    """给 Agent 看的结果。"""
    lines = [f"在「{machine}」上执行完（{r.get('took')} 秒），" +
             (f"超过 {r.get('timeout')} 秒没跑完，已停掉" if r.get("killed") else f"退出码 {r.get('code')}") + f"，工作目录 {r.get('cwd')}"]
    if r.get("stdout"):
        lines.append("标准输出：\n" + r["stdout"])
    if r.get("stderr"):
        lines.append("标准错误：\n" + r["stderr"])
    if not r.get("stdout") and not r.get("stderr"):
        lines.append("（没有输出）")
    got = save_files(r.get("files"))
    if got:
        lines.append("取回的文件（本机路径，可以直接打开）：\n" + "\n".join(
            f"- {src} → {local}" if local else f"- {src}：{why}" for src, local, why in got))
    return "\n\n".join(lines)
