---
name: feishu-handoff
description: Move the current Claude Code conversation to Feishu / Lark so the user can keep chatting from their phone, and bring it back when they return, using ccim. Use when the user is leaving their computer and wants to continue in Feishu or Lark ("我要出门了，转到飞书", "转到飞书", "在飞书上接着聊", "hand this off to Feishu", "continue on Lark"), or is back at the computer and wants what they did in Feishu brought into this conversation ("我回来了", "把飞书那边的接回来", "飞书上聊了什么", "I'm back, pull in the Feishu chat").
---

# Hand off to Feishu / Lark, and bring it back

[ccim](https://github.com/lzy2025ai/ccim) connects a project directory to a Feishu / Lark bot.

- `ccim handoff` branches this conversation into the user's direct message with the bot. The bot messages the
  user first with a recap, so their phone gets a notification and they can carry on from there.
- Feishu then continues as an independent branch. Whether to bring it back is the user's call: when they send a
  message here later, the plugin's hook notices new Feishu content and asks you to check with them first.
  `ccim handback` brings it over.

Both commands read `CLAUDE_CODE_SESSION_ID`. If it isn't set (for example, you're not Claude Code), tell the user
that only Claude Code conversations can be handed off, and stop.

If `ccim` isn't installed (`command -v ccim`), ask the user before installing it:

```bash
uv tool install --compile-bytecode git+https://github.com/lzy2025ai/ccim
```

ccim needs macOS, Python 3.11+, uv, and a Feishu or Lark account.

## Leaving: hand off

1. Run `ccim handoff` from the project directory of this conversation.
   - If the project already has a bot, it finishes within a few seconds.
   - If not, it opens a page with a QR code in the user's browser and **waits up to 10 minutes** for them to scan
     it with the Feishu / Lark mobile app. Run it in a way that can wait that long (e.g. in the background) and,
     while it waits, tell the user to scan the QR code that just opened in their browser. It also prints a link
     they can open in Feishu on their phone if scanning doesn't work.
2. Tell the user, in plain words:
   - The bot has sent them a message in Feishu with a recap — open it and keep chatting there.
   - Feishu continues from a branch of this conversation, with the same model and effort level.
   - When they're back and send a message here, you'll ask whether to bring over what they said in Feishu.

## Back: hand back

Do this when the user asks for it ("我回来了", "把飞书那边的接回来"), or when the hook tells you Feishu has new
content and the user says yes to bringing it over. If they say no, just answer their message and don't bring it up again.

1. Run `ccim handback` in this conversation. It prints each turn from Feishu since the handoff — what the user
   said, what Claude did (files read and edited, commands run) and its reply — then the files that were edited.
   It also tells the user in Feishu that the conversation has moved back to the computer.
2. Read the output carefully: treat it as part of this conversation. Files may have changed since you last saw them,
   so re-read any file before editing it. Commands run in Feishu can change files that aren't in the list; check
   `git status` if it matters.
3. Give the user a short recap of what happened in Feishu, then carry on.

## When it fails

Relay the message ccim prints; it says what to do. Common cases:

- **"正在一个终端里前台运行，而且是旧版本"**: ccim is running in a terminal with an older version. The user has to
  press Ctrl+C in that terminal (you can't stop it for them), then run `ccim handoff` again. Suggest
  `ccim start --always` afterwards, so future updates can restart it automatically.
- **"没有配对成功"**: the QR code wasn't scanned in time or was declined. Run `ccim handoff` again for a new code,
  or have the user pair manually in a terminal by running `ccim` in the project directory.
- **"这个对话没有转到过飞书"** (from `handback`): nothing to bring back. If they were chatting with the bot without a
  handoff, they can continue that chat in a terminal with `ccim resume`.
