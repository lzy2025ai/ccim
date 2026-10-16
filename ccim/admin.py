"""用管理员权限执行命令（#12）：Agent 调 run_as_admin 工具，用户确认后执行。

网页（只在本机）：渠道的 ask_secret 弹一张卡片，用户输开机密码点「执行」，密码直接从标准输入交给 sudo -S，
不进对话记录、不进日志，也不回给 Claude。
飞书、微信（消息要经过它们的服务器）：不收密码，用 osascript 的 with administrator privileges
在 Mac 上弹系统自己的授权窗口。
"""
import asyncio, logging

log = logging.getLogger("ccim.admin")

TIMEOUT = 600                                 # 等用户确认
RUN_TIMEOUT = 600                             # 命令本身最多跑这么久
OUTPUT_LIMIT = 20000
SUDO = "/usr/bin/sudo"


def _cut(text):
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + "\n…（后面太长没显示）"


async def _sudo(command, password):
    p = await asyncio.create_subprocess_exec(SUDO, "-S", "-k", "-p", "", "--", "/bin/sh", "-c", command,
                                             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate((password + "\n").encode()), RUN_TIMEOUT)
    except asyncio.TimeoutError:
        p.kill()
        return False, "命令跑了太久，已经停掉。"
    finally:
        password = None
    err = err.decode(errors="replace")
    if p.returncode != 0 and ("incorrect password" in err or "Sorry, try again" in err or "no password was provided" in err):
        return False, "密码不对，命令没有执行。可以再调用一次让用户重新输入。"
    return p.returncode == 0, _cut(f"退出码 {p.returncode}\n{out.decode(errors='replace')}{err}")


def _applescript(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _dialog(command, reason):
    script = f"do shell script {_applescript(command)} with prompt {_applescript('ccim：' + reason)} with administrator privileges"
    p = await asyncio.create_subprocess_exec("/usr/bin/osascript", "-e", script, stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), TIMEOUT + RUN_TIMEOUT)
    except asyncio.TimeoutError:
        p.kill()
        return False, "等了太久没有确认，命令没有执行。"
    err = err.decode(errors="replace")
    if p.returncode != 0 and "-128" in err:
        return False, "用户在授权窗口里取消了，命令没有执行。"
    return p.returncode == 0, _cut(f"退出码 {p.returncode}\n{out.decode(errors='replace')}{err}")


async def run(chat, command, reason):
    """返回 (成功与否, 给 Claude 看的结果)。"""
    ch = chat.b.channel
    ask = getattr(ch, "ask_secret", None)
    if ask:
        pw = await ask(chat.chat_id, command, reason, TIMEOUT)
        if pw is None:
            return False, "用户没有同意（或者超时了），命令没有执行。"
        try:
            return await _sudo(command, pw)
        finally:
            pw = None
    await ch.send_text(chat.chat_id, f"要用管理员权限执行命令（{reason}），Mac 上弹出了授权窗口，请在电脑上确认。")
    return await _dialog(command, reason)
