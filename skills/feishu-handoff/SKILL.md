---
name: feishu-handoff
description: Move the current Claude Code conversation to Feishu / Lark so the user can keep chatting from their phone, using ccim. Use when the user is leaving their computer and wants to continue this conversation in Feishu or Lark, e.g. "我要出门了，转到飞书", "转到飞书", "在飞书上接着聊", "把这个对话发到飞书", "hand this off to Feishu", "continue on Lark".
---

# Hand off this conversation to Feishu / Lark

[ccim](https://github.com/lzy2025ai/ccim) connects a project directory to a Feishu / Lark bot. `ccim handoff`
branches the current Claude Code conversation into the user's direct message with that bot. The bot messages
the user first with a recap, so their phone gets a notification and they can carry on from there.

## Steps

1. **Check that this is a Claude Code conversation.** `ccim handoff` reads `CLAUDE_CODE_SESSION_ID`.
   If it isn't set (for example, you're not Claude Code), tell the user that only Claude Code conversations
   can be handed off, and stop.

2. **Check that ccim is installed:** `command -v ccim`. If it's missing, ask the user before installing:

   ```bash
   uv tool install --compile-bytecode git+https://github.com/lzy2025ai/ccim
   ```

   ccim needs macOS, Python 3.11+, uv, and a Feishu or Lark account.

3. **Run `ccim handoff`** from the project directory of this conversation.
   - If the project already has a bot, it finishes within a few seconds.
   - If not, it opens a page with a QR code in the user's browser and **waits up to 10 minutes** for them
     to scan it with the Feishu / Lark mobile app. Run it in a way that can wait that long (e.g. in the
     background) and, while it waits, tell the user to scan the QR code that just opened in their browser.
     The command also prints a link they can open in Feishu on their phone if scanning doesn't work.

4. **Tell the user the result in plain words.** On success:
   - The bot has sent them a message in Feishu with a recap — open it and keep chatting there.
   - Feishu continues from a branch of this conversation, with the same model and effort level.
     This conversation on the computer is not changed and won't see what they say in Feishu.
   - Back at the computer, they can continue the Feishu conversation in a terminal with `ccim resume`.

## When it fails

Relay the message ccim prints; it says what to do. Common cases:

- **"正在一个终端里前台运行，而且是旧版本"**: ccim is running in a terminal with an older version.
  The user has to press Ctrl+C in that terminal (you can't stop it for them), then you run `ccim handoff` again.
  Suggest `ccim start --always` afterwards, so future updates can restart it automatically.
- **"没有配对成功"**: the QR code wasn't scanned in time or was declined. Run `ccim handoff` again for a new code,
  or have the user pair manually in a terminal by running `ccim` in the project directory.
