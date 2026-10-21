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
- Claude 的消息由 `_pump` 一直读进 `Chat.inbox`，空闲时也读：后台任务（子 agent、后台命令）做完，Claude Code 会自己开一轮，
  `_next` 读到就当一轮处理（AWAKE，不提问、没有 reply_to），回答单独发出去。以前空闲时不读，这一轮积着，下一条消息先读到它（秒回），之后每条错一位
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
- `skills/ccim-setup/`：教 agent 帮用户接入飞书 / 微信（AGENTS.md 指向它）。改了接入流程、命令或报错文字要同步改它，并升 plugin.json 的 version。
  agent 跑命令时不在终端里：二维码走 `qrpage.show`（不在终端就在浏览器里打开），需要确认的地方要有 `--yes`，不能卡在 input 上；
  `ccim` 本身保持前台运行（用户要的），agent 用 `ccim start --always`（没配对会先配对）或 `ccim --weixin`
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
- `ccim/asks.py` + `ccim ask`：agent 派任务。和 handoff 一样走状态目录（ccim 进程登记的那个：飞书是项目，微信是微信号）里的
  asks/<编号>.json，进程每秒取；`Bridge.ask` 在聊天里发「[谁 派的任务] …」、按普通消息排队（不插进正在做的一轮，
  因为要单独拿结果）。任务是 `session.Task`（str 子类，带 future），`Chat._finish` 记下 outcome，`_work` 结束时交给它；
  /stop 清队列时也会结束。命令行等结果时看 status 里的进程号，进程没了就不再等。退出码 0 / 2 / 3 / 4 / 1
- 智能体平台（#7，方案 docs/智能体平台-产品方案.md）：`ccim web` 打开。
  - `ccim/agents.py`：Agent 登记表 ~/.ccim/agents.json（名字、印章、角色说明、底座、默认模型和思考深度、工作目录可空）。
    `avatar` 可选内置头像 ID（`AVATARS` 白名单），空值沿用文字印章；图片在 `ccim/static/avatars/`，由平台和 Hub 共用的 `static_file` 提供。
    Agent 是设置的唯一来源，改了同步写回它关联的飞书登记、微信号 projects（agent_id 指回来；`_sync`）；命令行改项目设置用
    `absorb` 记回 Agent。老「项目」在任意 ccim 命令时 `migrate` 成 Agent。角色说明经 `session.role_note` 进系统提示（飞书、微信也生效）
  - `ccim/platform.py`：平台进程（登记表 kind=platform，路径 ~/.ccim/platform，daemon / 安全重启照常用）。asyncio 主循环跑对话，
    HTTP（ThreadingHTTPServer）在线程里，用 run_coroutine_threadsafe 交回主循环；/api/events 是 SSE。
    `WebChannel`：cards=False、live=True —— Chat `_tick` 每 0.3 秒推 progress（步骤、正在写的字），`_finish` 先推最终步骤再 send_text，
    渠道把两者合成一条 answer。每个网页对话一份记录 web/<对话>.jsonl（user / answer / note / card / file）。
    网页对话的状态在平台 state.json 的 chats（agent 或 temp 设置、origin 等）。Agent 对话共用 Agent 的 Bridge；临时对话各一个 Bridge
  - 飞书、微信、终端里的对话：`registry.all_chats` + `list_sessions`，内容由 `ccim/history.py` 从 Claude Code / Codex 的对话记录读出来；
    网页里接着聊 = 新网页对话 fork（Codex 用 `codex exec fork`），原来那个照常列出
  - `ccim/web.py`：原控制台的渠道管理（启动停止、日志、断开、扫码接入 Job）被平台接口复用；接入成功后 `agents.link`
  - `ccim/web.html`：单页，原型在 docs/prototype/。界面不用浏览器原生控件（下拉、确认框都是自己画的）
- `ccim/projects.py`：项目（群聊，#8）。项目存平台 state.json 的 projects（只在主循环里改）；消息流记录 web/project-<编号>.jsonl
  （task、reply，reply 同一个 id 会被更新，`Logs.read` 按 MERGED 合并）。每个 Agent 在每个项目里一个网页对话 p-<项目>-<Agent>
  （meta 里 project / cwd，平台对话列表里不显示），派活 = 往这个对话里放 `session.Task`，同一 Agent 自然排队、Agent 之间看不到彼此。
  对比且项目目录是 git 仓库：每人 `git worktree`（~/.ccim/worktrees/，分支 ccim/<任务>-<Agent>）+ 新对话 p-…-<任务>，
  做完自动提交到分支；「用这个」= squash 合并进项目目录当前分支记成一次提交，项目目录有没提交的改动时拒绝，冲突就 reset 回去。
  回答还没出来时状态是 queued，网页按「这个对话正在干活、而且是它最早没做完的一件」显示成干活中；平台重启后没做完的显示 lost
- `ccim/schedules.py`：定时任务（#9）。存平台 state.json 的 schedules（next_run 存盘），运行记录 web/schedule-<编号>.jsonl（run 按 id 合并）。
  平台进程里每 30 秒检查一次：到点就跑，下一次从「现在」往后算，所以错过几次都只补一次（比预定晚 5 分钟以上标 catchup）；
  只跑一次的跑完就暂停。给 Agent：网页对话 s-<任务>-<运行>（或 session=same 时一直用 s-<任务>），meta 里 schedule，不进对话列表；
  给项目：走 `projects.dispatch`（一起做），等各人做完。deliver 用 `channels.send_once` 私聊主人
- 分叉（`Platform.fork_chat`）：每轮回答记下 `fork`（会话编号 + 这一轮最后一条回答的 uuid，`Turn.uuid`），新网页对话 parent 指回去、
  fork_from / fork_at 存在状态里（`send` 里不能把 fork_from 当参数传，否则 fork_at 被丢掉，变成从整个对话分），之前的记录复制过去。
  Codex 只能从整个对话分，所以只有最后一条回答能分叉。没选目录的临时对话分叉后用原来的临时目录（temp.scratch），会话记录按目录存。
  对话列表按 parent 挂成树（接着飞书、微信对话聊的，parent 是 x:<原会话>）
- 对话列表（#18）：终端里分叉出来的对话（Claude Code 分叉会把之前的消息原样、编号不变地复制过去）按共用的消息认出从哪个分出来
  （`history.fork_parents`，读对话记录的消息编号，按文件只读新增部分并缓存；平台启动时 `_warm` 先读一遍）。终端对话取最近 40 个认分叉，
  只列最近 15 个和它们的上级。列表里的标记（重命名、置顶 pin 越小越靠上、归档、图标、颜色）：网页对话存在对话 meta 里，飞书 / 微信 / 终端的对话
  存平台 state 的 marks（删除 = hidden，只从列表里去掉）。分叉没单独置顶、归档时跟着原来那个在同一组；树最多缩进两层
- 退回（#24）：网页里的 Claude 会话开着文件快照（enable_file_checkpointing）并让它回显我们发的消息（replay-user-messages），
  每轮记下提问的编号（回答记录里的 prompt）。退回到第 i 条 = 文件 `rewind_files` 到第 i 条之后那个回答的 prompt，
  对话接到第 i 条之前那个回答的 fork（另开分支，和分叉一样；之前没有回答就开新会话），记录截到第 i 条（`Logs.truncate`）。
  连接、断开都在 worker 里，所以是往队列里放 REWIND 信号。Codex 不支持；之前那个回答没有 fork（没答完）的退不了。
  只有编辑工具（Write / Edit）改的文件有快照，命令行改的恢复不了；确认框先用 dry_run 算出会恢复哪些文件再列出来
- 章节（#25）：消息按「类型:时间」（chapKey）标识，网页对话存在对话 meta 的 chapters，飞书 / 微信 / 终端的存在 marks 里（不进列表数据，
  只在 external_detail 里带上）。对话页 `.chat-mid` = 消息 + 右侧目录（主区域够宽、没开成员栏时显示，否则头部按钮弹出列表）
- 归档清理（#26）：天数存在每台电脑的全局设置 archive_days（7 / 30 / 90 / never，默认 30），网页上改时逐台保存（/api/archive_days）。
  归档时记 archived_at；到期 = max(归档时间, 最后一条消息) + 天数（`delete_at`），平台每小时 `cleanup_archived`：网页对话 delete_chat，
  别处的对话在 marks 里 hidden。列表里最后一周显示「还有 N 天删除」
- `ccim/kb.py`：知识库（#27）。~/.ccim/kb/<库>/_lib.json + 每条一个 .md（第一行注释是编号、标题、来源、修改时间，后面「# 标题」和正文，
  助手直接用 Grep / Read 查）。删除记在 _tombstones.json。每台电脑一份完整的：平台 `_kb_sync_loop` 每 30 秒（本机改了就 `kb_changed`
  叫别的电脑马上来）问在线的其他电脑要清单（/api/kb/manifest），比自己新的用 /api/kb/export 拉过来（`wanted` / `apply`），后改的算数；
  服务器只转发。网页读写本机这份（S.me）。挂到对话：对话 meta 的 kbs → WebChannel.kbs_of → `Chat._connect` 里 add_dirs + `kb_note`
  （只有 Claude Code）；改了挂哪些要 reconnect。条目的 source 记来源对话（machine、chat、at），网页按 at 找到那条消息定位（`doJump`）
- 响应速度（#32）：别的电脑的 state 用缓存（peers.cache_at，60 秒内直接用，超过 10 秒后台再取），往那台发过 POST 或收到它的推送
  （正在干活的进度除外）就标 dirty、下次现问。history.read 按文件大小和修改时间缓存；`_external` 用最近列表里的那行，不再列一遍所有对话。
  网页：api() 有请求超过 150 毫秒显示顶部进度条，按下的按钮进入处理中（防连点）；发消息先把自己那条显示出来（sending），
  推送到了再换掉；新对话 `starting` 防重复创建；长对话先画最近 60 条（SHOW_RECENT），跳转到更早的会先展开；列表宽度 --list-w 可拖动
- 文档预览（#34，`ccim/docs.py`）：对话里的 .md / .html / .docx（Agent 发的、用户上传的）点开在右侧面板（手机全屏，`DOC`、`drawDoc`）。
  Agent 每发一次存副本（日志目录 snaps/<对话>/，记录里的 snap），面板切版本；最新的一版读原文件。文档里相对路径引用的图片
  （HTML 还有样式表、脚本）由 `docs.view` 打包成 data: 地址，所以别的电脑、服务器上也能显示，HTML 放 sandbox iframe 的 srcdoc。
  Markdown 用 `docMd`（比聊天的 md() 全，每块带 data-l 原文行号）。保存（/api/doc/save）带打开时的 mtime，文件被改过就拒绝；
  记一条 type=edit，对话 meta 的 doc_edits 在下一次发消息时拼在前面告诉助手。Word 自己转（`docx_html`），只能看
- `ccim/upgrade.py`：版本和网页上升级（#37）。`info` 认出是开发版（代码目录，git 提交）、稳定版（~/.ccim/stable 目录名）、
  发布版（uv tool 装的，direct_url.json 里的提交）还是服务器（~/ccim-hub/deployed.txt）；发布版和 GitHub 上最新提交比（后台问，一小时一次）。
  state 里的 ccim → machine_info 带到网页。升级由独立进程（`python -m ccim.upgrade run`）做：uv tool upgrade → 用新装的 python 问新提交 →
  逐个 safe_restart 在跑的项目（平台最后）→ 平台没起来就 `uv tool install --force git+…@旧提交` 再重启。uv 会换掉整个环境，
  所以这个进程开头就把 daemon、registry、runtime 都导入好。结果在 ~/.ccim/upgrade.json
- `ccim/board.py`：任务面板（#48）。对话记录扫成摘要（最后回答的时间和状态、有没有等批准、每天发了几条，按文件大小和修改时间缓存）；
  chat_view 带 attention（done / ask / err，回答晚于 meta 的 seen_at 算没看过；网页打开对话就 /api/chat/seen）、brief（一句话进度：
  WebChannel 写了回答就 on_answer → `refresh_brief` 用 haiku 总结，推送 type=brief）、doing。第一次启动时已有对话都算看过（board_seen_init）。
  /api/board 是概览（近 26 周每天条数、本周轮数和完成数、各 Agent 本周条数），网页逐台取再合起来；等你处理 / 在干活 / 最近在跟由 S.chats 算
- `ccim/mentions.py`：网页对话里 @ 其他 Agent（#11）。每个被 @ 的 Agent 一个子对话 m-<对话>-<Agent>（meta 里 mention_of，不进列表），
  再被 @ 时接着它的会话，只补上它上次（ctx_upto）之后对话里的新内容；结果记在主对话里（type=mention，按 id 合并）。
  之后不带 @ 发消息时，主对话的助手没看过的回答（done_at > mention_seen）拼在消息前面交给它。删主对话连子对话一起删
  群聊里的 Agent 自己也能 @ 群成员：`ask_agents` 工具（session.py `_ask_agents`，只给网页里 Agent 的 w- 对话）→ `Mentions.ask`，
  记录带 by（网页显示成它说的），等各人回答完（waiters，最多 20 分钟）把回答作为工具结果交回，所以 unseen 不再重复带
- `ccim/admin.py`：管理员权限（#12）。私聊里给 Claude 一个 `run_as_admin` 工具（在 allowed_tools 里，它自己让用户确认）。
  渠道有 `ask_secret`（只有网页：本机）时弹卡片看命令、输开机密码，密码从标准输入交给 `sudo -S -k`（每次都要输），
  不进记录、不进日志、不回给 Claude；卡片 type=secret 按 id 合并，平台重启后没处理的显示作废。
  飞书、微信不收密码，用 osascript `with administrator privileges` 在 Mac 上弹系统授权窗口。网页重画时保留正在输的密码
- `ccim/peers.py`：多台电脑（#13）。没加入别人的是总控，`ccim join 地址 配对码` 加入（~/.ccim/hub.json；总控记在 peers.json，
  本机编号和名字在 machine.json）。各台电脑的平台照旧管自己的东西，网页只在本机打开；网页请求带 X-Machine（`<img>` 用 machine 参数），
  平台不是自己的就 `forward`，那边照原样请求自己的 127.0.0.1 接口（带 X-From-Peer），所以各接口不用改。/api/state 拼起来（每项带
  machine，另有 machines / me / machine_info），离线的用 peer-cache.json。连接方向是加入的电脑主动连总控（局域网只开 PEER_PORT 8978，
  有电脑加入或生成了配对码才开）：总控→它靠长轮询 /peer/poll 取请求和推送，它→别处 POST /peer/call，推送 /peer/push，_m 记来源不回传。
  每个请求和回应都 HMAC 签名（时间戳 + 随机数 + 内容摘要）。请求一直没被取走就撤回并算离线（免得回来重复执行）；发消息给离线的电脑
  先存 outbox.json，连上再发。从别的电脑来的密码请求一律拒绝，只能选「在那台 Mac 上弹授权窗口」（admin.DIALOG）；/api/pick、/api/reveal
  不接受别的电脑来的。网页按 id 找东西在哪台电脑（`ownerOf`），找不到用 ctxMachine（当前打开的东西所在的电脑）。
  测试：两个临时 HOME 各跑一个平台进程（registry.HOME、registry.REG、peers.PEER_PORT 都要改掉，REG 不改会写进真的登记表），一个 join 另一个
- 界面布局（#14，视觉稿 docs/prototype/multi-machine.html）：Agent、项目没选中具体哪个时是卡片总览（`.app.cards` 去掉中间那列），
  顶部电脑标签（machineTab，localStorage 记住）。进入 Agent：#/agents/<编号>/<对话>，左边是它的对话（/api/agent 详情里的 chats，含终端，
  去掉项目、定时、@ 子对话），设置在 #/agents/<编号>/settings；对话页只放临时对话，打开 Agent 的对话会跳到 Agent 页。Agent 置顶存 agents.json 的 pinned。
  项目可以拉别的电脑上的 Agent：dispatch 时 `peers.agent_home` 找到它在哪台，`_run_remote` 调那台的 /api/peer/task（开对话 p-<项目>-<Agent>，
  马上回一个编号），再隔 5 秒问 /api/peer/result；进度靠推送实时过来。那台离线就等它上线（12 小时），它看不到项目目录、不参与 git 副本
- `ccim/groups.py`：群聊（#15，取代项目入口；项目页还在，只是导航里没有了）。群聊 = Agent 的网页对话 meta 里有 members（除主 Agent）、
  note（群聊说明，note_at）。不 @ 时主 Agent 回答；@ 走 mentions（@ 到不在群里的顺手拉进来，别的电脑上的走 /api/peer/task kind=mention），
  各人看到哪记在主对话 meta 的 ctx_upto。群聊说明改过后各人下一次被问到时在消息前面带上（note_seen）。
  启动时 `migrate_projects` 把每个项目复制成群聊（只做一次，state 的 groups_from_projects）：第一个本机成员当主 Agent 并 fork 它在项目里的会话，
  记录转成 user / answer / mention，指向项目的定时任务改成 kind=group
- `ccim/hubserver.py`：服务器上的总控（#16，`ccim hub --url https://IP`）。只做网页、登录、转发：Peers(server=True) 不把自己算一台电脑，
  /peer/* 和网页各听 127.0.0.1 的 8978 / 8977，前面 nginx 做 HTTPS（Let's Encrypt IP 证书，acme.sh shortlived 自动续）。
  登录：密码 scrypt + TOTP（同一个码不能用两次），第一次用 setup 链接（`ccim hub setup-link`）设置；Cookie 只存摘要，接口还要 X-CCIM 头；
  输错锁定（同地址 15 分钟 5 次、全部一小时 20 次）。/api/state 拼各台电脑的（me 是第一台在线的电脑，hub 字段标明在服务器上），
  其余接口按 X-Machine 转发；开机密码、选文件夹、在访达里显示一律不经过服务器。网页里 S.hub 时每台电脑都按「别的电脑」处理。
  电脑那边 `ccim join https://IP 配对码`（hub.json 里 server=true，_conn 走 HTTPS、用 certifi 的根证书）。
  部署（服务器地址等记在本机，不写进仓库）：~/ccim-hub/venv（pip install --no-deps 源码 + qrcode、certifi），systemd ccim-hub，nginx sites-available/ccim-hub
- 能选的模型（`ccim/models.py`）：Claude Code 的列表来自连一次会话时 initialize 里的 models（不发消息，要用和对话同一个 claude，
  不同版本别名指向不同型号），平台每天刷新，存 ~/.ccim/models.json；「更多模型」（旧版本）来自 Claude Code 按账号缓存的
  ~/.claude/cache/model-catalog/*-cc.json（section=overflow），和桌面端模型菜单一致。Codex 读它自己的 ~/.codex/models_cache.json
- 连不上 Claude 时 Claude Code 会发 `SystemMessage(api_retry)`（attempt），`Turn.retry` 记下，网页进度和飞书卡片显示「网络不稳，正在重试」
- `ccim/history.py`：把 Claude Code / Codex 的对话记录读成 user / answer 记录；`title` 取第一句话给老对话当标题
- `ccim/progress.py`：工具调用转成一行中文
- `ccim/registry.py`：`~/.ccim/registry.json` 登记表、钥匙串、每个项目的 state.json（各对话的 session_id、模型、思考深度等）；
  读 Claude Code 自己的设置（`claude_defaults`），只用于显示
- `ccim/daemon.py`：后台启动（脱离终端）、常驻（`~/Library/LaunchAgents/com.ccim.<key>.plist`，KeepAlive SuccessfulExit=false：
  ccim stop → SIGTERM → 退出码 0 不拉起；崩溃、启动失败退出码非 0，30 秒后拉起）、在线判断、关停
- `ccim/channels/base.py`：渠道接口；卡片用和渠道无关的 dict 描述，渠道自己渲染
- `ccim/channels/feishu.py`：飞书。扫码建机器人走未公开的设备码注册接口（做法参考 NousResearch/hermes-agent）；
  lark-oapi 长连接跑在单独线程（它用模块级事件循环，要换成线程自己的），事件用 run_coroutine_threadsafe 交回主循环；
  按钮回调必须同步返回，所以 on_action 在主循环里算好新卡片，回调里等最多 2.5 秒

- `ccim/channels/weixin.py`：个人微信，微信官方 ClawBot 接口（iLink，没有公开文档，做法参考 hermes-agent 的 weixin 接入）。
  扫码登录拿 bot_token（存钥匙串），扫码的人就是主人；长轮询 getupdates 收消息，游标和每个人的 context_token
  存在状态目录的 weixin.json（游标丢了会重放旧消息，回复要带 context_token）。只有私聊；消息发出去改不了，
  所以 `cards=False`：工作中卡片换成每 5 秒续一次「正在输入」，审批卡片当文字发、回复 y / n。图片文件走微信 CDN，AES-128-ECB
- `ccim/weixin_hub.py`：一个微信号只能有一个 ClawBot，所以微信是「一个号管多个项目」。登记表里每个微信号一条，
  键是 `~/.ccim/weixin/<名字>`（kind=weixin，projects 记它管的项目），daemon、安全重启、start / stop / logs 都把它当项目路径用。
  每个号一个进程（`Hub`），每个项目一个 Bridge，`/项目` 切换；不是当前项目的回答加「【项目名】」（`ProjectChannel`）。
  各项目在微信里的会话记录存在这个号的 state.json（`HubStore`），不和飞书进程抢项目的 state.json。
  `registry.all_chats(path)` 汇总一个项目在各渠道的对话（show、resume、/resume、查看工具用）。
  `ccim --weixin` 总是扫码，扫的哪个微信就加到哪个；`ccim --weixin 名字` 不扫码直接加（和用户约好的）。
  再扫同一个微信（按 ilink_user_id 认）是重新登录，项目保留；登录凭证变了要重启这个号的进程。
  重新扫码后、用户发消息之前，机器人主动发的消息微信会静默丢掉（接口照样返回成功），所以扫码加项目时不发提醒，
  而是直接切到新项目，提示写进 state 的 notice，用户发下一条消息时先发出去；不扫码加项目则不切、主动提醒0.9.0 的「项目直接接微信」在任意命令时自动迁移
- 查看其他对话：私聊里给 Claude 两个只读工具 `list_chats` / `read_chat`（session.py `_look_around`），在 allowed_tools 里免审批；
  群聊不给（群里别人会借它看主人的私聊）。Codex 没有
- 渠道按需导入（`channels.load`），用微信的进程不加载飞书 SDK

## 设计取舍

- **权限用 auto 模式**。实测过：auto 模式下普通操作（包括 rm）直接放行，auto 判定要拦的直接拒绝，都不走 `can_use_tool`；
  只有少数必须由人拍板的操作才会弹审批卡片。
- **模型和思考深度默认不传**，交给 Claude Code 按项目、全局设置自己取，这样飞书和终端里打开同一个对话用的是同一套。
  思考深度不记在对话记录里，是每次启动会话时传的，写死默认值会让两边不一致。
  用户自己设了才传，优先级：聊天里 /model /effort（state 的 chats）> 项目 `ccim config`（登记表）> `ccim config --global`
  （~/.ccim/config.json，只管 Claude Code 项目，Codex 的模型名和档位不一样）> 不传。`Chat.default` / `Chat.follow`（/model 默认）
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

## 开发流程（和用户约好的，#19、#42）

用户会同时和好几个 Agent（Claude Code 会话、网页群聊里的 Agent）提需求，大家并行开发，所以：

- **仓库主目录（本机网页平台、ccim 测试机器人跑的就是它）永远停在 main，任何人都不在里面切分支、改代码。**
- 每个需求：
  1. 提 issue（仓库 lzy2025ai/ccim）：做什么、第一版要有的（勾选清单）、先不做的。issue、PR、提交说明都是公开的，不写服务器地址、
     用户名、内部路径、客户和项目名，措辞面向外部读者。
  2. 建自己的工作目录再开工：
     `git -C <主目录> fetch origin && git -C <主目录> worktree add ~/.ccim/worktrees/ccim-issue-<编号> -b issue-<编号>-<简称> origin/main`，
     之后所有读写、提交都在这个目录里。跑测试用主目录的 .venv：`PYTHONPATH=<工作目录> <主目录>/.venv/bin/python …`（不另装环境）。
  3. 提交说明带 `#编号`，推上去开 PR（描述写 `Closes #编号`），然后告诉用户可以验收。
- **验收**：`ccim try <编号>` 把本机跑开发版的进程（网页平台、ccim 测试机器人）切到那个工作目录上（安全重启，起不来用稳定版顶上），
  `ccim try` 看正在试哪个，`ccim try main` 切回。同一时间只试一个；切之前先看有没有对话正在干活（`ccim show`），会被打断。
  用户在飞书里体验也一样：`ccim try <编号>` 之后回复用户（重启会断掉当前这一轮）。
- **用户说验收通过**：合并 PR（squash），issue 自动关闭；没验收的不合并。main 开了保护：PR 要基于最新的 main 且检查通过才能合并，
  别的 PR 先合了就在自己的工作目录里 `git fetch && git rebase origin/main`、解决冲突、强推，再合并。合并后：
  `git -C <主目录> pull --ff-only` → `ccim try main`（不管有没有在试它，都要跑）→ `ccim promote`（一律按最新的 main 装稳定版，
  升级跑稳定版的项目）→ 删掉工作目录（`git worktree remove …`）和分支。不要直接 `ccim restart` 别的项目到开发版。
  `ccim try main` 会重启本机跑开发版的进程（网页平台、ccim 测试机器人），它们才用上刚合并的 Python 代码。web.html 每次请求现读，
  刷新网页就是新的；后台没重启时，新网页配旧后台，新功能会悄悄失效（#61 合并后删终端对话就这样没生效）。
  重启会打断正在进行的对话（包括正在和用户说话的这一轮），所以先告诉用户、等用户同意再跑。
- 合并进 main 后 GitHub Actions（.github/workflows/deploy.yml）先用 Python 3.11 检查，再把服务器上的总控更新到这个版本。
  别的电脑在网页「设置 → 电脑」里点升级（#37）。
- 部署：Actions 用 `git archive` 打包从标准输入交给服务器，部署钥匙在服务器 authorized_keys 里限定成只能跑
  ~/ccim-hub/hub-update.sh（仓库里的 deploy/hub-update.sh，改了要手动拷上去），新版本起不来就装回上一个，版本记在 deployed.txt。
  仓库 Secrets：DEPLOY_SSH_KEY、DEPLOY_KNOWN_HOSTS、DEPLOY_HOST（用户@地址；不能放仓库变量，变量会原样打印在公开的运行日志里）。只有 lzy2025ai/ccim 会部署，fork 不会
- 用 gh 操作前先 `gh auth switch --user lzy2025ai`，做完切回 `gh auth switch --user flowstart`（推送同理）。
- 提交时只 `git add` 自己改的文件，不要 `git add -A`。
- 重启一律交给独立进程（#70）：`ccim try` 用 `schedule_restart`；ccim 进程启动时设环境变量 CCIM_PROCESS，在它里面（比如平台里的 Agent）
  `ccim restart` 它自己会自动改成独立进程。直接在当前进程里先停再启动，停的时候会把执行命令的 Agent 一起结束，启动就没人做了。
- 实现：`runtime.set_try` 把要试的工作目录记在 ~/.ccim/stable/channel.json 的 try，`launch_env` 在启动开发版进程时把它放到 PYTHONPATH
  最前面（daemon 的后台启动和 LaunchAgent 都带上）；`build_stable` 按 origin/main 那次提交装。

## 约定

- 要能在 Python 3.11 上跑（pyproject 写的 >=3.11，别的电脑可能就是 3.11）：f-string 的 {} 里不能有反斜杠、不能用和外层相同的引号。
  改完用 3.11 过一遍：`for f in ccim/*.py ccim/channels/*.py; do python3.11 -c "import ast;ast.parse(open('$f').read())"; done`

- 卡片、回复、命令行输出里只写用户需要的信息，不写实现细节。
- 本地验证不需要飞书：写个假渠道（实现 send_text / send_card / update_card / delete_message / add_reaction / send_file / download），
  在临时目录建项目，把 `registry.HOME` 指到临时目录，直接调 `Bridge.on_message`，就能跑真实的 Claude 会话。
