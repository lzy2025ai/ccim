# ccim

在飞书里和本机的 Claude Code 对话。每个项目目录配一个飞书机器人，在飞书里发消息，
就相当于在这个目录里用 Claude Code：项目的 CLAUDE.md、skill、斜杠命令都能用，用的是本机 `claude` 的登录。

## 需要

- macOS（密钥存钥匙串、常驻靠 launchd、防睡眠用 caffeinate）
- Python 3.11 以上，以及 [uv](https://docs.astral.sh/uv/)
- 已安装并登录的 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（终端里能直接运行 `claude`）
- 飞书或 Lark 账号

## 安装

```bash
uv tool install --compile-bytecode git+https://github.com/lzy2025ai/ccim
```

装好后 `ccim` 命令在 `~/.local/bin` 里。升级：`uv tool upgrade ccim`。

## 开始

```bash
cd 你的项目目录
ccim
```

第一次运行会显示二维码，用飞书手机端扫码确认，就会在你的飞书账号下建好一个机器人并配对。
之后再运行 `ccim` 直接连上。在飞书里搜机器人的名字，私聊它就能开始。

扫码失败时可以改为手动填写 App ID 和 App Secret（在飞书开放平台自建应用，开启机器人能力，
事件与回调都选「长连接」，订阅「接收消息」「机器人进群」事件和「卡片回传交互」回调）。
手动配对后，终端会给出一串认领口令，在飞书里私聊机器人发送这串口令，机器人就认你为主人。

## 命令

| 命令 | 作用 |
|---|---|
| `ccim` | 在当前目录前台运行，Ctrl+C 退出。没配对过就先扫码 |
| `ccim start [项目]` | 后台运行：关掉终端、关掉 Claude 桌面版都不影响 |
| `ccim start --always [项目]` | 常驻：开机登录后自动启动，崩溃或断网启动失败会每 30 秒自动重启 |
| `ccim stop [项目]` | 关停；常驻的同时取消常驻 |
| `ccim restart [项目]` | 重启（常驻的重启后仍然常驻） |
| `ccim list` | 所有已配对的项目：渠道、机器人、在线还是离线、前台 / 后台 / 常驻 |
| `ccim show [项目]` | 一个项目的详情：机器人、状态、模型、思考深度、群聊权限、各个对话 |
| `ccim resume [项目] [对话编号] [--fork]` | 在终端里接着飞书里的对话聊；不写编号就接最近聊过的。飞书那边还开着这个对话时自动另开分支 |
| `ccim logs [项目] [-f]` | 查看后台日志，`-f` 持续输出 |
| `ccim config [项目] 键=值` | 项目设置，见下文 |
| `ccim unpair [项目]` | 解除配对，删掉钥匙串里的密钥 |

`[项目]` 可以写项目目录名、机器人名或完整路径，不写就是当前目录，所以在任何目录都能开关某个项目。

## 在飞书里

- **私聊**：和你的私聊是一个长期对话，ccim 重启后接着聊。`/new` 重新开始。
- **工作中卡片**：Claude 动手干活时会出一张卡片，每两三秒刷新它最近在做什么；做完变绿，最终回答单独发一条消息。
- **审批**：Claude Code 用 auto 权限模式，大多数操作自己判断放行或拦下；少数必须由人拍板的操作会弹审批卡片，
  点「允许」「本会话都允许」「拒绝」，或者直接回复 `y` / `n`。10 分钟没处理按拒绝算。
- **发文件给它**：图片、文件会存到项目的 `.ccim/inbox/`（里面自带 `.gitignore`，不会被提交），Claude 能直接看。
- **让它发文件**：比如「把封面发我」，它会把图片、视频、文件发到聊天里（飞书限制：图片 10 MB、文件 30 MB）。
- **话题**：对某条消息「回复话题」，Claude 的回复、进度卡片、发回的文件都留在这个话题里，不打扰主对话。
  每个话题是主对话的一个分支：记得开话题之前聊过的内容，也知道话题是针对哪条消息开的；
  话题里聊的不会带回主对话。在话题里发 `/new` `/stop` 只影响这个话题。
- 发出的消息会被加上一个「OnIt」表情，表示收到、正在处理，回复发出后去掉。
- 同一个聊天（或话题）里的消息排队处理，不同聊天、不同话题可以同时进行。空闲 30 分钟后会话自动休眠，下条消息再接上。
- 有任务在跑时 Mac 不会睡眠。

### 斜杠命令

| 命令 | 作用 |
|---|---|
| `/new` | 开始新对话 |
| `/resume [编号]` | 不写编号：列出这个项目最近的对话（终端里开的也在）；写编号：接上那个对话，另开分支，原来那边不受影响 |
| `/stop` | 停下正在做的事，清空排队 |
| `/status` | 状态、模型、思考深度 |
| `/model 名字` | 换模型：`opus` `sonnet` `haiku` `fable`，或完整模型名 |
| `/effort 等级` | 思考深度：`low` `medium` `high` `xhigh` `max` |
| `/help` | 帮助 |

其他斜杠命令原样交给 Claude，所以项目里的 skill 和 `/compact` 都能用。

## 群聊

把机器人拉进飞书群，这个群就是一个全新的对话，不带私聊的内容。在群里要 @机器人 它才处理。

默认只响应你本人。想让群里其他人也能 @ 它干活：

```bash
ccim config 项目名 group=all      # 群里所有人都能用
ccim config 项目名 group=owner    # 改回只认你
ccim restart 项目名               # 正在运行的要重启才生效
```

开放给别人之前想清楚：群里的人能让 Claude 在你电脑上的这个项目里读写文件、运行命令（auto 模式会拦下明显危险的操作，
但不是万无一失）。审批按钮始终只认你本人；群聊里让 Claude 发文件，只能发项目目录里的。

## 其他设置

```bash
ccim config model=sonnet              # 这个项目的默认模型：opus、sonnet、haiku、fable
ccim config effort=high               # 这个项目的默认思考深度
ccim config reaction=THUMBSUP         # 收到消息时加的表情（默认 OnIt），名字见飞书开放平台的表情文案说明
ccim config                           # 查看当前设置
```

不设的话跟随 Claude Code 自己的设置（项目的 `.claude/settings*.json`，其次是 `~/.claude/settings.json`），和终端里用的一致。
聊天里用 `/model`、`/effort` 改的只对那个聊天生效。

## 安全

- 只响应配对时扫码的那个人（主人）；私聊里别人发消息一律不理，群聊默认也只认主人。
- App Secret 存在 macOS 钥匙串（服务名 `ccim`），不写进任何文件。
- 权限用 Claude Code 的 auto 模式，不是完全放开。

## 文件位置

- `~/.ccim/registry.json`：已配对的项目（路径、渠道、机器人、主人、设置）
- `~/.ccim/projects/<编号>/`：每个项目的运行状态（进程号、各聊天的会话编号）和后台日志 `ccim.log`
- `<项目>/.ccim/inbox/`：飞书里收到的图片和文件

## 已知限制

- 只支持 macOS 和飞书 / Lark。
- 扫码建机器人用的是飞书的设备码注册接口，这个接口不在飞书公开文档里，将来可能变动；
  失效时可以按上面的说明手动填写 App ID 和 App Secret。
- 界面和提示目前只有中文。

## 开发

```bash
git clone https://github.com/lzy2025ai/ccim && cd ccim
uv venv && uv pip install --compile-bytecode -e .
ln -sf "$PWD/.venv/bin/ccim" ~/.local/bin/ccim
```

代码结构、设计取舍和本地测试方法见 [CLAUDE.md](CLAUDE.md)。接其他 IM（比如企业微信）：在 `ccim/channels/` 里实现
`base.py` 的 `Channel` 接口，在 `cli.py` 的 `CHANNELS` 里登记。

## 致谢

扫码建机器人、长连接收发卡片的做法参考了 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
的飞书接入。

## 许可证

[MIT](LICENSE)
