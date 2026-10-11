---
name: ccim-setup
description: Connect a project on this Mac to Feishu / Lark or WeChat with ccim, so the user can chat with Claude Code or Codex in that project from their phone. Use when the user wants to use Claude Code or Codex from Feishu, Lark or WeChat ("把这个项目接到飞书", "接入微信", "我想在微信里用 Claude Code", "用飞书控制 Codex", "connect this project to Feishu", "use Claude Code from WeChat"), wants to add another project or another WeChat account, or asks why a ccim bot isn't responding. For moving the current conversation to the phone, use the feishu-handoff skill instead.
---

# Connect Claude Code / Codex to Feishu or WeChat

[ccim](https://github.com/lzy2025ai/ccim) runs on the user's Mac and connects a project directory to a chat app.
Messages sent in the app go to Claude Code (or Codex) working in that directory, and the replies come back.

- **Feishu / Lark**: each project gets its own bot. Private chats, groups (@ the bot), topics, live progress cards.
- **WeChat** (personal, via WeChat's official ClawBot): one WeChat account has only one bot, so one WeChat
  account serves several projects and the user switches with `/项目` in the chat. Private chat only.
- The same directory can be connected to Feishu and WeChat at the same time; each is a separate conversation.

Requirements: macOS, Python 3.11+, [uv](https://docs.astral.sh/uv/), Claude Code installed and logged in
(for Codex: the ChatGPT desktop app or the Codex CLI, logged in), and a Feishu / Lark or WeChat account on their phone.

## Before you start

1. Check whether ccim is installed: `command -v ccim`. If not, ask the user before installing:

   ```bash
   uv tool install --compile-bytecode git+https://github.com/lzy2025ai/ccim
   ```

2. See what's already connected: `ccim list` (Feishu bots and WeChat accounts) and `ccim weixin` (WeChat accounts
   and their projects). Don't pair again what's already there.
3. Work out from the user, if it isn't clear:
   - **Which project directory.** Run the commands from inside it. Don't use the home directory or `/` unless the
     user explicitly wants that (the home directory needs `--yes`, and gives the assistant access to all their files).
   - **Feishu or WeChat.**
   - **Claude Code or Codex.** Claude Code is the default; add `--agent codex` for Codex. For Codex, first check it's
     there: `ls /Applications/ChatGPT.app` or `command -v codex`. If neither exists, tell the user to install the
     ChatGPT desktop app (or the Codex CLI) and log in. Being installed isn't enough: ask the user whether they're
     logged in (with the CLI you can check `codex login status`); otherwise the first message will fail.

## Feishu / Lark

```bash
cd <project>
ccim start --always            # add --agent codex for Codex
```

- If the project isn't paired yet, this shows a QR code first and **waits up to 10 minutes** for the user to scan
  it with the Feishu / Lark mobile app. When the command isn't run in a terminal (your case), the QR code opens in
  the user's browser. Run the command so it can wait that long (e.g. in the background), and tell the user to scan
  the QR code that just opened in their browser. Scanning creates a bot under their Feishu account.
- After pairing it keeps running in the background, starts at login and restarts itself if it crashes. The command
  returns once it's online; the last line says 「已常驻」. While it waits for the scan, poll `ccim list` every few seconds
  (the project appears once the scan succeeds) rather than relying on reading the running command's output.
- Already paired and want to switch to Codex (or back)? `ccim restart <project> --agent codex` (`--agent claude`).
- Don't run bare `ccim` for the user: it runs in the foreground and never returns. That's meant for someone
  watching their own terminal.

Then tell the user: in Feishu, search for the bot's name (shown by `ccim list`) and send it a message.

## WeChat

```bash
cd <project>
ccim --weixin                  # add --agent codex for Codex
```

- This always shows a QR code (in the browser when not run in a terminal) and waits up to about 8 minutes for the
  user to scan it with WeChat; an expired code is replaced automatically. Run it so it can wait (e.g. in the
  background). **Whichever WeChat account scans it, the project is added to that account.** Scanning an account
  that's already logged in doesn't log in again; it just adds the project, and the chat switches to the new project.
  The last line says 「已加到微信「名字」里」 (or 「本来就在…」 if the project was already there). While it waits,
  poll `ccim weixin`: the project shows up under an account once the scan succeeds.
- `ccim --weixin <name>`:
  - If an account with that name is logged in: adds the project to it **without scanning**. The chat stays on its
    current project and the user gets a reminder in WeChat.
  - If there's no account with that name: shows a QR code and logs in a **new** account under that name, then adds
    the project — one command does both. Tell the user which phone to scan with ("用你的工作微信扫"). If they scan
    with an account that's already logged in, ccim refuses and adds nothing (「扫码的是已经登录过的微信」);
    run the same command again and have them scan with the other WeChat.
- New accounts without a name are called 「微信」, then 「微信2」, 「微信3」…
- `ccim weixin` lists the accounts, e.g.:

  ```
  微信「微信」　在线（常驻）
    shop（当前）　/Users/me/work/shop
    blog　/Users/me/work/blog
  微信「工作号」　在线（常驻）
    report　/Users/me/work/report
  ```
- Each WeChat account runs in the background permanently; nothing else to start.

Then tell the user: in WeChat, open the 「微信ClawBot」 chat and send a message. `/项目` lists the projects,
replying with a number switches. Replies from a project that isn't the current one start with 「【项目名】」.

More WeChat commands:

| Task | Command |
|---|---|
| Log in another WeChat account, no project yet | `ccim weixin login <name>` |
| Rename an account | `ccim weixin rename <old> <new>` |
| Remove a project from WeChat | `ccim weixin remove <project>` (`--from <name>` for one account only) |
| Log out an account | `ccim weixin logout <name>` |

## Check that it works

- `ccim list` should show the project (Feishu) or the WeChat account as 在线.
- `ccim show <project>` shows the bot, the assistant, the model, and the conversations.
- Ask the user to send a message from their phone.

## When something goes wrong

Relay what ccim prints; it says what to do. Common cases:

- **"没有配对成功" / QR code expired**: the user didn't scan in time or declined. Run the command again for a new code.
- **"启动失败" or the bot doesn't reply**: look at `ccim logs <project or WeChat account name>`, then
  `ccim restart <name>`.
- **"微信登录已失效"** (shown in `ccim list`): run `ccim weixin login <name>` and have the user scan again.
  Projects stay.
- **"你登录了好几个微信号"**: add the account name: `ccim --weixin <name>`.
- **"这里是用户主目录"**: you're in the home directory. `cd` to the project, or add `--yes` only if the user really
  wants the assistant to work across their whole home directory.
- **WeChat images fail to send** with an upload error: large images can fail when the Mac's connection to WeChat's
  file servers is slow; smaller images or a better network help.

Stopping: `ccim stop <name>`. Unpairing a Feishu bot: `ccim unpair <project>`.
