# ccim

在飞书里和本机的 Claude Code 对话。用法见 README.md；这份文件写给改代码的人（和 Claude）。

## 结构

- `ccim/cli.py`：命令入口；`run()` 是前台、后台、常驻三种方式共用的主循环（连渠道、写心跳、等退出信号）
- `ccim/bridge.py`：收到消息后的分流：认主人、权限（主人 / 群聊 group=all）、群里要 @、话题、下载附件、斜杠命令、排队
- `ccim/session.py`：`Chat` = 一个对话地址对应一个 Claude 会话。worker 协程独占 `ClaudeSDKClient`（连接、提问、断开都在里面做，
  跨协程断开会出 anyio 的错，所以 /new、/resume 是往队列里放 RESET / ATTACH 信号，由 worker 自己处理）；
  空闲 30 分钟断开，按 session_id 恢复；工作中卡片；`send_file` 工具
- 任务进行中又来消息（bridge `_ask_followup`）：和 Claude Code 一样直接补充（在这一轮里再 `client.query`），消息上加表情，
  不弹卡片；想打断让用户先发 /stop。实测：补充会在下一个工具结果时一起交给模型，进同一轮结果；来得晚（最后一个工具结果之后）时，
  这一轮结束后再等 INJECT_GRACE 秒，Claude 接着处理就读到下一个结果，没处理就排到队首重跑。
  /stop 时还有 Claude 没看到的补充（`t.unseen`）：Claude Code 打断后会自己开一轮处理它，要把那一轮也打断、读完扔掉（`t.drain`），
  否则它的回答会落到下一条消息头上；打断产生的「已中断」工具结果不能当成「已看到」
- 一轮的结束不能只看第一个 `ResultMessage`：接上会话时 Claude Code 可能先补一轮（被杀掉的后台任务的通知），回答为空，
  要接着读（`STRAY_WAIT`）。session_id 在 `SystemMessage(init)` 时就存，第一轮被打断也能接上。见 docs/排查-2026-10-03-*.md
- `ccim/codex.py`：`CodexChat`（继承 `Chat`，项目 `agent=codex` 时 bridge 用它）。一轮 = 一次 `codex exec --json`（接着聊加 `resume <thread_id>`），
  标准输入必须关掉否则一直等。只换了「怎么和助手说话」：`_turn_inner` / `_interrupt`（SIGINT）/ `inject`（恒 False，exec 不接受中途补充，
  `codex queue` 放进去它也看不到）；进度卡片、排队、发回答（`Chat._finish`）都复用。发文件靠回复里的「[发送文件] 路径」行。
  codex 优先用 ChatGPT.app 自带的（不依赖 Node，后台进程里也能跑），模型等跟随 ~/.codex/config.toml。
  对话编号存的键按助手分开（`Chat.SID_KEY`：session_id / codex_session_id），`--agent` 切换后两边互不干扰
- `ccim/approval.py`：`can_use_tool` → 审批卡片 → 按钮 / 文字 y n / 10 分钟超时
- `ccim/commands.py`：`/new` `/resume` `/stop` `/status` `/model` `/effort` `/help`，其他斜杠命令原样交给 Claude
- `ccim/handoff.py`：`ccim handoff`，把 Claude Code 里正在进行的对话（`CLAUDE_CODE_SESSION_ID`）转到飞书私聊。
  CLI 往项目状态目录写 handoff.json，ccim 进程每秒取一次；用 `resume_session_at` 只接到「用户说要转过去」之前的最后一条回复，
  不带上转接这一轮；没私聊过就用 open_id 主动发消息拿到 chat_id
  没配对过就地配对：二维码做成网页在浏览器里打开（在 Claude 桌面端里用户看不到命令行输出）；
  正在运行的 ccim 是旧版本（state.json 的 caps 里没有 handoff）时，后台 / 常驻的自动重启，前台的提示用户去终端重启
  `ccim handback`：handoff 时在飞书聊天的状态里记下 handoff_from / handoff_time；接回来时找到那个分支，
  按时间戳取转过去之后的内容（fork 出来的对话记录里，复制过来的旧消息保留原时间戳），整理成文字打印，再在飞书里发提醒。
  飞书那边是独立分支，带不带回来由用户决定：`ccim/hook.py`（插件 hooks/hooks.json 的 UserPromptSubmit 钩子，命令 ccim-hook）
  只检测、让 Claude 先问，不自动带。两个进度：synced_to_desktop（handback 才推进）、notified_to_desktop（同一批只问一次）。
  飞书那边正在回复的一轮（聊天状态里 busy）先不带。钩子每条消息都跑，不能 import 飞书 SDK
- `skills/feishu-handoff/`：配套 skill，告诉 Agent 什么时候、怎么转接；`.claude-plugin/` 让这个仓库能作为 Claude Code 插件安装。
  改了 handoff 的行为或提示文字，要同步改 SKILL.md（它引用了几条报错原文）；改了 skill 要升 plugin.json 的 version，
  已安装的插件才会更新（插件按版本号判断）
- `ccim/runtime.py`：稳定版 / 开发版、安全重启、`ccim promote`。稳定版在 `~/.ccim/stable/<版本>/`（独立 venv，从某次提交装），
  `channel.json` 记 current / previous；项目的 `runtime`（dev / stable）、`stable_version`（钉版本）在登记表里。
  启动命令带 `-P`，否则在 ccim 代码目录里跑稳定版会导入目录里的开发代码。安全重启由独立进程做（优先用稳定版跑），
  新版本 60 秒内没连上或很快退出就用稳定版顶上（state 里 fallback=True），结果主动私聊主人。
  venv 的 python 是指向系统 python 的软链，判断属于哪个环境要看目录（`_env`），不能 realpath python 本身
- `ccim/usage.py`：`/usage` 中文版。额度来自 SDK 的 RateLimitEvent（raw 里的 unifiedWindows），每轮对话记到 `~/.ccim/rate.json`；
  明细是临时会话跑 Claude Code 自带 /usage 再按句式翻译。新会话没发过请求时 /usage 不带额度，所以缓存旧了先用 haiku 问一句
- `ccim/progress.py`：工具调用转成一行中文
- `ccim/registry.py`：`~/.ccim/registry.json` 登记表、钥匙串、每个项目的 state.json（各对话的 session_id、模型、思考深度等）；
  读 Claude Code 自己的设置（`claude_defaults`），只用于显示
- `ccim/daemon.py`：后台启动（脱离终端）、常驻（`~/Library/LaunchAgents/com.ccim.<key>.plist`，KeepAlive SuccessfulExit=false：
  ccim stop → SIGTERM → 退出码 0 不拉起；崩溃、启动失败退出码非 0，30 秒后拉起）、在线判断、关停
- `ccim/channels/base.py`：渠道接口；卡片用和渠道无关的 dict 描述，渠道自己渲染
- `ccim/channels/feishu.py`：飞书。扫码建机器人走未公开的设备码注册接口（做法参考 NousResearch/hermes-agent）；
  lark-oapi 长连接跑在单独线程（它用模块级事件循环，要换成线程自己的），事件用 run_coroutine_threadsafe 交回主循环；
  按钮回调必须同步返回，所以 on_action 在主循环里算好新卡片，回调里等最多 2.5 秒

## 设计取舍

- **权限用 auto 模式**。实测过：auto 模式下普通操作（包括 rm）直接放行，auto 判定要拦的直接拒绝，都不走 `can_use_tool`；
  只有少数必须由人拍板的操作才会弹审批卡片。
- **模型和思考深度默认不传**，交给 Claude Code 按项目、全局设置自己取，这样飞书和终端里打开同一个对话用的是同一套。
  思考深度不记在对话记录里，是每次启动会话时传的，写死默认值会让两边不一致。
- **对话地址**：普通聊天是 chat_id；话题是「chat_id|thread_id」，往这个地址发的消息都进话题。
  话题第一次开口时 fork 主对话的会话（`fork_session`），所以记得之前的内容，之后互不影响。
- **接别处的对话时 fork**：飞书里 `/resume` 一律 fork；`ccim resume` 在飞书那边还开着这个对话时 fork。避免两个进程同时往一个对话里写。
- **飞书事件**：没注册处理函数的事件 SDK 会报 processor not found，飞书还会反复重推；表情、撤回、已读、进出群都注册了空处理。
- **SDK 单条消息上限**默认 1 MB，Claude 读图片时会超，放宽到 64 MB（只是解析时的瞬时内存）。

## 稳定性

- 飞书接口：10 秒超时（文件 120 秒），网络错误 / 限流 / 5xx 重试两次；发消息带 uuid，飞书一小时内按它去重，重试不会重复
- 最终回答几次都没发出去就转后台每 20 秒补发（同一个 uuid），最多 10 分钟
- 先发回答再收尾卡片，卡片接口卡住不拖累回答
- 长连接：lark 自己断线重连；整个客户端退出了，启动成功过的话等 10 秒重建
- 环境里设了 `PYTHONDONTWRITEBYTECODE` 时，lark-oapi 一万多个文件每次启动都要重新编译（十几秒），所以安装时加 `--compile-bytecode`

## 开发流程（和用户约好的）

- ccim 自己的测试机器人（项目 ccim）跑开发版；其他项目跑稳定版。你很可能就运行在 ccim 测试机器人里。
- 改完想让用户在飞书里体验：先提交，再 `ccim restart --safe ccim`，然后回复用户（重启会断掉当前这一轮，默认 15 秒后才重启，
  留时间把回复发出去）。新版本起不来会自动用稳定版顶上，飞书里会收到通知，回来接着修。
- 用户说验收通过，再 `ccim promote` 升级其他项目；不要直接 `ccim restart` 别的项目到开发版。
- **同一个机器人里可能有好几个对话（私聊主对话、各个话题）同时在改这个仓库。** 提交时只 `git add` 自己改的文件，
  不要 `git add -A`：会把别的对话改到一半的代码一起提交、一起重启上线。
- **重启前先看别的对话是不是正在干活**（`ccim show` 里的对话、state.json 的 busy），正在干活的会被打断；必要时先问用户。

## 需求管理（GitHub issue）

- 用户确认要做的需求，提一个 issue（仓库 lzy2025ai/ccim）：写清做什么、第一版要有的（勾选清单）、先不做的。
- 做的时候提交说明里带 `#编号`；用户验收通过后，推送时在提交说明里写 `Closes #编号`，issue 自动关闭。没验收的不关。
- 用 gh 操作前先 `gh auth switch --user lzy2025ai`，做完切回 `gh auth switch --user flowstart`（推送同理）。

## 约定

- 卡片、回复、命令行输出里只写用户需要的信息，不写实现细节。
- 本地验证不需要飞书：写个假渠道（实现 send_text / send_card / update_card / delete_message / add_reaction / send_file / download），
  在临时目录建项目，把 `registry.HOME` 指到临时目录，直接调 `Bridge.on_message`，就能跑真实的 Claude 会话。
