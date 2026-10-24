# ccim

在飞书里和本机的 Claude Code 对话。用法见 README.md；这份文件写给改代码的人（和 Claude）。

## 结构

- `ccim/cli.py`：命令入口；`run()` 是前台、后台、常驻三种方式共用的主循环（连渠道、写心跳、等退出信号）
- `ccim/bridge.py`：收到消息后的分流：认主人、权限（主人 / 群聊 group=all）、群里要 @、话题、下载附件、斜杠命令、排队
- `ccim/session.py`：`Chat` = 一个对话地址对应一个 Claude 会话。worker 协程独占 `ClaudeSDKClient`（连接、提问、断开都在里面做，
  跨协程断开会出 anyio 的错，所以 /new、/resume 是往队列里放 RESET / ATTACH 信号，由 worker 自己处理）；
  空闲 30 分钟断开，按 session_id 恢复；工作中卡片；`send_file` 工具
- 任务进行中又来消息（bridge `_ask_followup`）：和 Claude Code 一样直接补充（在这一轮里再 `client.query`），消息上加表情，
  不弹卡片；想打断让用户先发 /stop；想排到后面发 `/queue 内容`（也认 /排队，#79。网页对话在 `Platform.send` 拼前缀之前认出来，用 Incoming.queue 带过去）。实测：补充会在下一个工具结果时一起交给模型，进同一轮结果；来得晚（最后一个工具结果之后）时，
  这一轮结束后再等 INJECT_GRACE 秒，Claude 接着处理就读到下一个结果，没处理就排到队首重跑。
  /stop 时还有 Claude 没看到的补充（`t.unseen`）：Claude Code 打断后会自己开一轮处理它，要把那一轮也打断、读完扔掉（`t.drain`），
  否则它的回答会落到下一条消息头上；打断产生的「已中断」工具结果不能当成「已看到」
- 卡住提醒（#109）：Turn.flight 记还没出结果的顶层工具调用，t.last 记最近一次收到 Claude 的任何消息；有工具在等、STALL（5 分钟）里没任何动静
  就算卡住（`Turn.stalled`）：网页进度 snap 带 stall（对话列表、任务面板、群聊这一行按 `Platform._stall` 连同 @ 子对话显示），飞书卡片加一行，微信发一句。
  「结束这一步」（网页按钮 /api/chat/skip、/skip、/跳过）= `Chat.skip`：interrupt_with 一条「这一步被结束了，不要原样重试，接着做」，
  卡着期间的补充（t.unseen）一起写进去；派来的 Task 把 future 交给接着做的这一轮（carried），不当成停下
- 一轮的结束不能只看第一个 `ResultMessage`：接上会话时 Claude Code 可能先补一轮（被杀掉的后台任务的通知），回答为空，
  要接着读（`STRAY_WAIT`）。session_id 在 `SystemMessage(init)` 时就存，第一轮被打断也能接上。见 docs/排查-2026-10-03-*.md
- Claude 的消息由 `_pump` 一直读进 `Chat.inbox`，空闲时也读：后台任务（子 agent、后台命令）做完，Claude Code 会自己开一轮，
  `_next` 读到就当一轮处理（AWAKE，不提问、没有 reply_to），回答单独发出去。以前空闲时不读，这一轮积着，下一条消息先读到它（秒回），之后每条错一位
- `ccim/codex.py`：`CodexChat`（继承 `Chat`，项目 `agent=codex` 时 bridge 用它）。一轮 = 一次 `codex exec --json`（接着聊加 `resume <thread_id>`），
  标准输入必须关掉否则一直等。只换了「怎么和助手说话」：`_turn_inner` / `_interrupt`（SIGINT）/ `inject`（恒 False，exec 不接受中途补充，
  `codex queue` 放进去它也看不到）；进度卡片、排队、发回答（`Chat._finish`）都复用。ccim 的工具经 MCP 挂上（`mcp_args`，见下面 toolsock），
  老对话里约定过的「[发送文件] 路径」行照旧认。
  codex 优先用 ChatGPT.app 自带的（不依赖 Node，后台进程里也能跑），模型等跟随 ~/.codex/config.toml。
  对话编号存的键按助手分开（`Chat.SID_KEY`：session_id / codex_session_id），`--agent` 切换后两边互不干扰
- `ccim/toolsock.py` + `ccim/mcpstdio.py`：ccim 的工具和底座无关（#178）。`Chat._tool_list` 是这个对话能用的工具（Claude Code 照旧进程内挂上）；
  每个 ccim 进程开一个只有本用户能连的 unix socket（~/.ccim/run/tools-<pid>.sock），会话环境变量带 CCIM_TOKEN（认出是哪个对话）和 CCIM_TOOLS（socket），
  调的就是这个对话自己那份工具，权限、审批和 Claude Code 里一样。mcpstdio.py 是标准 MCP 服务（stdio，不 import ccim，用 `python -I` 跑），
  Codex 每一轮用 -c mcp_servers.ccim.* 挂上：令牌要写进 env（Codex 不传自己的环境变量给 MCP 服务），工具要设自动放行
  （exec 里没人批准，不设就被拒），等待时间放长（问别的 Agent 要等它答完）。不支持 MCP 的底座用 `ccim tool [名字 'JSON']`。
  Codex 不给 send_to_chat（转交目标是 Claude Code 会话）
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
  - `ccim/agents.py`：Agent 登记表 ~/.ccim/agents.json（名字、印章、角色说明、底座、默认模型和思考深度、工作目录可空、chrome 开关）。chrome 开了 `session._connect` 给 CLI 加 `--chrome`（只对 Claude 底座，#90）；改了开关，`relaunch_agent` 让它开着的网页对话下一条消息时按原会话续接、换上新参数。
    `avatar` 可选内置头像 ID（`AVATARS` 白名单），空值沿用文字印章；图片在 `ccim/static/avatars/`，由平台和 Hub 共用的 `static_file` 提供。
    Agent 是设置的唯一来源，改了同步写回它关联的飞书登记、微信号 projects（agent_id 指回来；`_sync`）；命令行改项目设置用
    `absorb` 记回 Agent。老「项目」在任意 ccim 命令时 `migrate` 成 Agent。角色说明经 `session.role_note` 进系统提示（飞书、微信也生效）
  - `ccim/platform.py`：平台进程（登记表 kind=platform，路径 ~/.ccim/platform，daemon / 安全重启照常用）。asyncio 主循环跑对话，
    HTTP（ThreadingHTTPServer）在线程里，用 run_coroutine_threadsafe 交回主循环；/api/events 是 SSE。
    `WebChannel`：cards=False、live=True —— Chat `_tick` 每 0.3 秒推 progress（步骤、正在写的字），`_finish` 先推最终步骤再 send_text，
    渠道把两者合成一条 answer。每个网页对话一份记录 web/<对话>.jsonl（user / answer / note / card / file）。
    网页对话的状态在平台 state.json 的 chats（agent 或 temp 设置、origin 等）。Agent 对话共用 Agent 的 Bridge；临时对话各一个 Bridge
  - 飞书、微信、终端里的对话：`registry.all_chats` + `list_sessions`，内容由 `ccim/history.py` 从 Claude Code / Codex 的对话记录读出来；
    飞书、微信里的对话在网页里接着聊 = 新网页对话 fork（Codex 用 `codex exec fork`），原来那个照常列出
    终端、桌面端里的 Claude 对话（#94）接着原会话聊、不分支：新网页对话 meta 里 session_id = origin、continued_at，列表里只剩它（`_linked`）。
    实测：Claude 桌面端开着一个对话时挂着带 --resume 的 claude 进程，内容在内存里，不重新读记录，那边一开口就会和网页写的分叉；
    所以 `history.open_elsewhere` 看进程，开着就在网页顶部醒目提示（不拦）。别处后来说的从原会话记录里读（按提问 / 回答编号去掉网页自己的，
    `elsewhere`），网页停在这类对话上每 5 秒问 /api/chat/sig，变了就重新取。网页发消息前 `Chat._stale`：记录里最后一条不是自己上一轮的回答，
    就断开重新接上。终端对话、转过来之前的回答都能分叉（history 记录带 uuid，`_fork_history`）
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
  只跑一次的跑完就暂停。给 Agent：默认 session=same，一直用网页对话 s-<任务>-<Agent>（#132，`schedules.listed`：进对话列表、带「定时」标签，
  用户能接着问；每次运行走 `_run_chat`，记成 sched 提问并加章节，连续两次出错先 reset 会话）；session=new 每次 s-<任务>-<运行>，不进对话列表。meta 里都有 schedule；
  给项目：走 `projects.dispatch`（一起做），等各人做完。deliver 用 `channels.send_once` 私聊主人
  Agent 自己建（#122）：`ccim schedule`（cli.py，走本机平台的 HTTP 接口）。会话启动时给助手进程环境变量 CCIM_CHAT / CCIM_CHANNEL / CCIM_AGENT /
  CCIM_PROJECT（`Chat.ccim_env`），PATH 前面加 ~/.ccim/bin（只放 ccim 一个软链，`ccim_bin`）。网页对话里建的是 kind=chat：到点在原对话里跑（`_run_chat`），
  记录里那条任务带 sched；飞书、微信里建的交给 Agent、deliver 回那个渠道。quiet：提示里让它没事只回 QUIET，WebChannel 不记这种回答，
  `Logs.drop` 去掉那条任务、推送 drop。频率 every（每隔几分钟）。Claude 的 CronCreate / CronList / CronDelete 在 ccim 会话里禁用
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
- `ccim/skills.py`：平台 Skill（#101）。~/.ccim/skills/lib/<名字>/ + meta/<名字>.json（来源 github / upload / local、停用、修改时间），
  删除记 _tombstones.json；同步复用知识库那套（`_kb_sync_loop` 里第二组，/api/skills/manifest、export，整个 Skill 打成 tar.gz）。
  挂载：`Chat._connect` 按 `b.entry["agent_id"]` 取 Agent，`plugin_for` 生成 views/<Agent>/ 本地插件（plugin.json 名字 platform，skills/ 下软链到 lib，
  只放没停用、Agent 的 skills_off 里没有的），SDK `plugins` 挂上，Claude 里是 platform:名字，不和本地 Skill 冲突；没变就不重建。只有 Claude Code。
  安装：GitHub（codeload 下 tar.gz，地址可以指到仓库里某个文件夹，找出所有带 SKILL.md 的文件夹）、上传（zip / .skill / tar.gz / SKILL.md）、
  从某台电脑的 ~/.claude/skills 导入（请求发到那台电脑，装进它的平台库再同步）。解压时拦掉往外写的路径和软链。网页 #/skills：装好后留在列表；详情左边文件树、右边看文件（Markdown 预览 / 源码，代码用 hlCode 简单高亮，图片直接显示），能改能存（/api/skills/file/save 带打开时的 mtime，改完算这个 Skill 更新、同步）。Agent 设置里勾挂哪些
  Agent 的本地 Skill（#115）：Agent 页「Skill」标签，`agent_skills` 按 `_groups` 分组（工作目录 .claude/skills、~/.claude/skills、开着的插件带的只读；
  Codex 是 ~/.codex/skills），接口 /api/agent/skill*（按 id 转到 Agent 所在的电脑）。查看编辑和平台 Skill 共用 SKV（src.kind platform / local），
  软链接过去的保存前提示改的是原件（原地写）。「提交成平台 Skill」= `agent_share` → `_put`（同名替换，source 记 agent）。
  Skill 助手（#116）：Skill 页右侧对话栏（`skContent` 把 #main 分成内容和 #sk-chat，收起状态存 localStorage，手机上是全屏层）。
  背后是每台电脑一个的网页对话，meta 里 helper=skills（`Platform.helper_chat`，/api/helper，不进对话列表、任务面板），
  `_bridge` 按 HELPERS 给它角色说明和 entry.helper，`session._skill_tools` 给它 skill_list / files / read / write / install / enable / delete；
  写文件走 `skills.write_any`（没有的新建），删除先弹审批卡片（approvals.ask）。改了平台 Skill 都调 `skills.changed()`：
  叫别的电脑同步、推送 type=skills 让网页刷新。发消息带 ctx（正看着哪个 Skill、哪个文件），拼在交给助手的话前面，不进记录。
  助手在哪台电脑上跑（`skcMid`）：localStorage 记的；没选过用总控那台，再不行用 S.me；标题下一行显示电脑 · 模型 · 思考深度，点开换（模型、思考深度走 /api/chat/set）；那台离线时提示并给「换到 xx」
  市场：`market` 查 skills.sh / ClawHub / SkillsMP 的公开搜索接口，名字相同的算，同一个 GitHub 仓库的优先（same），结果缓存 6 小时；网页显示安装、下载、仓库星数，没收录的链到市场搜索
- 响应速度（#32）：别的电脑的 state 用缓存（peers.cache_at，60 秒内直接用，超过 10 秒后台再取），往那台发过 POST 或收到它的推送
  （正在干活的进度除外）就标 dirty、下次现问。history.read 按文件大小和修改时间缓存；`_external` 用最近列表里的那行，不再列一遍所有对话。
  网页：api() 有请求超过 150 毫秒显示顶部进度条，按下的按钮进入处理中（防连点）；发消息先把自己那条显示出来（sending），
  推送到了再换掉；新对话 `starting` 防重复创建；长对话先画最近 60 条（SHOW_RECENT），跳转到更早的会先展开；列表宽度 --list-w 可拖动
- 文档预览（#34，`ccim/docs.py`）：对话里的 .md / .html / .docx（Agent 发的、用户上传的）点开在右侧面板（手机全屏，`DOC`、`drawDoc`）。
  Agent 每发一次存副本（日志目录 snaps/<对话>/，记录里的 snap），面板切版本；最新的一版读原文件。文档里相对路径引用的图片
  （HTML 还有样式表、脚本）由 `docs.view` 打包成 data: 地址，所以别的电脑、服务器上也能显示，HTML 放 sandbox iframe 的 srcdoc。
  Markdown 用 `docMd`（比聊天的 md() 全，每块带 data-l 原文行号）。保存（/api/doc/save）带打开时的 mtime，文件被改过就拒绝；
  记一条 type=edit，对话 meta 的 doc_edits 在下一次发消息时拼在前面告诉助手。Word 自己转（`docx_html`），只能看
- `ccim/filesend.py`：对话里的文件发给浏览器（#76）。本机的边读边发，支持 Range（206 / 416）和 ETag（304）。别的电脑上的（网页的 /api/file 带 machine，平台 `_remote` 和服务器 hubserver 都走 `serve_peer`）按段取 /api/file/part（一段 4 MB，回应是一行 JSON 文件信息 + 这一段），经现有转交通道（签名照旧），收到一段写一段、预取两段，所以没有大小上限；中途文件被改（ETag 变了）就断开连接。那边是旧版本（part 返回 404）时退回整个文件转发。链接、<img>、<video> 带不了 X-CCIM 头，所以 /api/file 在 `_remote` 里只校验 Host
- `ccim/devices.py`：设备管理（#100）。「设置 → 电脑」点一台进入 #/settings/machines/<编号>/<概况 | files | clean>，接口 /api/fs/*，
  按 X-Machine 转到那台电脑。范围是那台的主目录（realpath 后比较）；主目录本身、第一层的系统文件夹（KEEP）和 ~/.ccim 不能删、不能改名
  （网页上传留下的 uploads 除外）；删除 = 移到 ~/.Trash。预览、下载走 /api/file 和 /api/doc，chat 传 "@fs"（FS_CHAT），
  `_file_ok` / `doc_versions` 按设备范围放行，只认本网页发起的请求（Sec-Fetch-Site）。磁盘清理 `Cleaner`：后台线程扫描（缓存、很久没动的项目依赖、
  大文件、下载文件夹旧文件、废纸篓、旧上传），结果存 scan.json，清理只认结果里的路径；缓存和废纸篓清空内容，其余移到废纸篓。
  操作记在 ops.jsonl，概况页列最近的。「让 Agent 处理」打开新对话（工作目录是这个文件夹）或某个 Agent 的新对话，要处理的路径先写进输入框（prefill / draftText）
- `ccim/upgrade.py`：版本和网页上升级（#37）。`info` 认出是开发版（代码目录，git 提交）、稳定版（~/.ccim/stable 目录名）、
  发布版（uv tool 装的，direct_url.json 里的提交）还是服务器（~/ccim-hub/deployed.txt）；发布版和 GitHub 上最新提交比（后台问，一小时一次）。
  state 里的 ccim → machine_info 带到网页。升级由独立进程（`python -m ccim.upgrade run`）做：uv tool upgrade → 用新装的 python 问新提交 →
  逐个 safe_restart 在跑的项目（平台最后）→ 平台没起来就 `uv tool install --force git+…@旧提交` 再重启。uv 会换掉整个环境，
  所以这个进程开头就把 daemon、registry、runtime 都导入好。结果在 ~/.ccim/upgrade.json
- `ccim/board.py`：任务面板（#48）。对话记录扫成摘要（最后回答的时间和状态、有没有等批准、每天发了几条，按文件大小和修改时间缓存）；
  chat_view 带 attention（done / ask / err，回答晚于 meta 的 seen_at 算没看过；网页打开对话就 /api/chat/seen）、brief（一句话进度：
  WebChannel 写了回答就 on_answer → `refresh_brief` 用 haiku 总结，推送 type=brief）、doing。第一次启动时已有对话都算看过（board_seen_init）。
  /api/board 是概览（近 26 周每天条数、本周轮数和完成数、各 Agent 本周条数），网页逐台取再合起来；等你处理 / 在干活 / 最近在跟由 S.chats 算
- 公众号排版（#35）：Markdown 文档的面板多一个「公众号」视图（`wxView`）。`wxHtml` 把 `docMd` 的结果套上主题的内联样式
  （公众号编辑器去掉 class 和样式表，只认内联的；`WX_THEMES` / `wxCss`，主题记在 localStorage），外链改成文末参考资料，代码块的空格换行换成 &nbsp; / <br>。
  复制（`wxCopy`）去掉 data-*、class 后以 text/html 写剪贴板，图片是 data: 地址；每张图下面有复制图片（画到画布转 PNG）、下载。
  在排好版的页面上直接改（`blockClick`）：段落、标题、引用、列表用 contenteditable，失焦时 `blockMd` 转回 Markdown 换掉 data-l 那几行；
  表格、代码、图片弹原文小框。显示没变化的不算改（比较 docMd 结果）
- `ccim/mentions.py`：网页对话里 @ 其他 Agent（#11）。每个被 @ 的 Agent 一个子对话 m-<对话>-<Agent>（meta 里 mention_of，不进列表），
  再被 @ 时接着它的会话，只补上它上次（ctx_upto）之后对话里的新内容；结果记在主对话里（type=mention，按 id 合并）。
  之后不带 @ 发消息时，主对话的助手没看过的回答（done_at > mention_seen）拼在消息前面交给它。删主对话连子对话一起删
  对话里的助手自己找人（#179）：`platform_agents`（`_visible_agents`：本机的 + 别的电脑缓存里的，访客只有自己建的和共享给它的）、
  `ask_agent`（session.py `_ask_agent`，平台上的对话都有，Skill 助手除外；Codex 经 toolsock 一样有）。找别人 = `Mentions.send` 在当前对话里替它 @
  （被 @ 的成员找人就在外层群聊里 @），记录带 by（Agent 编号，没选 Agent 的对话是 ASSISTANT），网页 threadOrder 把它和回答排在这一轮回答前面；
  找自己 = `Platform.dispatch_self` 新开同样设置的对话（meta 的 dispatched_from，同一个 Agent 时挂在发起的对话下面）。
  wait 时等回答（最多 20 分钟）作为工具结果交回，所以 unseen 不再重复带；不等或没等到的，做完 `Platform.deliver` 作为新一轮交回发起的对话。
  被 @ 的成员（子对话）一定等。防失控：Task.depth（派出去的 +1，经 /api/peer/task 带到别的电脑），到 MAX_DEPTH 不能再派；一轮最多 MAX_ASKS 次
  子对话只装群里 @ 它的内容（#150）：用户在子对话里自己开口（发消息、@ 人、拉人）就 `Platform.split_sub` 另开这个 Agent 的独立对话
  （fork 子对话的会话，记录带过去，meta 的 split_from 指回外层群聊，网页跳过去）；子对话跟着外层群聊一起删，单独聊的不能挂在它下面。
  升级前在子对话里说过话、拉过人的，平台启动时 `_split_old_subs` 迁一次（成员、群聊说明、它里面 @ 过的人的本机子对话一起搬）
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
- 账号与共享电脑（#154，只在服务器总控上，默认关：`ccim hub accounts on` 打开，hub 的 settings.json；关着时只有主人能登录、没有账号页和共享，`Hub.accounts_on` / `Peers.multi`）：hubserver 的 accounts.json，主人 OWNER（老的 auth.json 迁过来，账号名 admin，登录时留空也算主人，
  密码 + 动态码）。主人在「设置 → 账号」建账号（`account_save`）：账号名 + 密码（默认 `new_password` 生成，只显示一次）、有效期、
  动态码可选（要的话给对方一次性链接绑验证器，`_setup_target` / setup_finish 的 bind）、停用。登录：账号名 + 密码，要动态码的回 need_code。
  电脑归配对码所属的账号（peers.json 的 account，没有就是主人）；`Peers.machines(account)`、`same_account` 让网页、转交、推送、/peer/call、
  fanout 都只在同一个账号内。主人按电脑给某个账号开权限：nodes 的 shares[账号] = {agents 共享哪些 Agent, own 能不能自己建, codex, dir 共享文件夹}
  （`clean_share`、`guest_of`），转过去的请求带 X-Account / X-Account-Name / X-Account-Share（浏览器带的不传），merged_state 按「电脑|账号」分开缓存；
  那台电脑还是旧版本（给访客的 state 里没有 guest）时不转。
  `ccim/share.py`：电脑那边按 X-Account（只认 X-From-Peer）当访客，每次来 `remember` 记到平台 state 的 guests（飞书、微信的进程读这里）；
  白名单接口（GUEST_GET / GUEST_POST），`check` 查东西是不是它的、能不能用（`_mine_agent` 自己建的 / `_usable_agent` 加上共享的），并改写工作目录
  （共享文件夹默认 ~/.ccim/shared/<账号名>，主人可以指定）。归属：Agent 记 account；对话 meta 的 account（没有就跟着 Agent；@ 子对话跟主对话，
  定时任务的对话跟任务）；定时任务记 account；知识库记 _lib.json（`owner_map` 缓存）。state、Agent 详情按人过滤（主人看不到访客的）；推送 `tag` 带 _a，
  总控 `visible` 只推给那个账号、fanout 不转给别的电脑。共享的 Agent：访客的对话单独一个 Bridge（key 加 #账号，entry lent），改不了设置、不能接飞书微信，
  会话里不给 list_chats / read_chat / send_to_chat；主人那边的 list_chats 也不列访客的会话。访客自己的 Agent 能接自己的飞书、微信（渠道按 `procs_of` 只管自己的）。
  访客的会话（`session.guest_of`）：不给 run_as_admin、不开 Chrome、不提示 ccim schedule；Codex 主人勾了才给，用时强制 -s workspace-write（只拦写不拦读，界面上提示）；
  `share.fence`：命令行沙箱（不许绕过，网络不限）+ 权限规则 deny 主目录里除 `allowed_dirs`（共享文件夹、自己 Agent 的目录、这个会话的工作目录、
  ~/.ccim/skills 的 lib、views 和它自己那份、自己的知识库）以外的一切，`_deny_except` 一层层往下只拦旁边的；send_file 只发这些地方的。仍以电脑主人的系统用户运行（防越界）
  访客的 Skill：`skills.SPACE`（contextvar，平台处理访客请求时设上、访客的 Skill 助手工具里设上）让 skills 的读写落到 skills/guests/<账号>/，
  不同步；`view_listing` / `reading` 让它看到主人那份里没停用的（shared，只读），`plugin_for` 给它的 Agent 挂两份（同名用它自己的）。
  Skill 助手每个访客一个（helper_chat 按 account，在共享文件夹里，role 加 GUEST_HELPER），只能从 GitHub 装
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
- 对话之间转交（#121）：平台上的对话（私聊、群聊里被 @ 的成员、定时任务）都有 `send_to_chat`（`_send_to_chat` → `Platform.relay`），
  只限同一个 Agent 的对话（跨 Agent 走群聊 @）；被 @ 的成员转交时来源记成那个群聊。目标按 list_chats 的会话编号找：
  已经有网页对话用着这个会话就交给它，终端、桌面端的 Claude 对话就像网页里接着聊一样新开一个网页对话（link）；飞书、微信的不转。
  `history.open_elsewhere` 说那边正开着就不写（会分叉），让 Agent 改用 Claude Code 自带的 SendMessage 或请用户转达。
  交过去的是 `session.Task`（排在手头这一轮后面，不插进去），记录 type=user 带 relay、from_chat；Task.relayed 的那一轮里再调用会被拒绝（防止来回转）
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
- 每个功能需求或 bug 走下面的流程；纯说明类的小改动（比如改 CLAUDE.md 里的一句话）直接在 main 上提交推送，不用提 issue。
- 每个需求：
  1. 提 issue（仓库 lzy2025ai/ccim）：做什么、第一版要有的（勾选清单）、先不做的。issue、PR、提交说明都是公开的，不写服务器地址、
     用户名、内部路径、客户和项目名，措辞面向外部读者。
     已关闭的需求没修好、漏了情况（同一件事的第二轮修改）：不新建，重开原来的 issue（`gh issue reopen`），在下面评论这次的问题和计划，
     分支、提交、PR 照样用原来的编号（分支名加 -2、-3 区分），PR 写 `Closes #原编号`，合并后它再次关闭。
     有几轮修改看 issue 的重开次数和关联的 PR 就知道。确实是另一件事的才新建，并在描述里写「和 #编号 有关」。
  2. 建自己的工作目录再开工：
     `git -C <主目录> fetch origin && git -C <主目录> worktree add ~/.ccim/worktrees/ccim-issue-<编号> -b issue-<编号>-<简称> origin/main`，
     之后所有读写、提交都在这个目录里。跑测试用主目录的 .venv：`PYTHONPATH=<工作目录> <主目录>/.venv/bin/python …`（不另装环境）。
  3. 提交说明带 `#编号`，推上去开 PR（描述写 `Closes #编号`），然后告诉用户可以验收。
- **验收**：`ccim try <编号>` 把本机跑开发版的进程（网页平台、ccim 测试机器人）切到那个工作目录上（安全重启，起不来用稳定版顶上），
  `ccim try` 看正在试哪个，`ccim try main` 切回。同一时间只试一个；切之前先看有没有对话正在干活（`ccim show`），会被打断。
  用户在飞书里体验也一样：`ccim try <编号>` 之后回复用户（重启会断掉当前这一轮）。
- **用户说验收通过**：合并 PR（squash），issue 自动关闭；没验收的不合并。main 开了保护：PR 要基于最新的 main 且检查通过才能合并，
  别的 PR 先合了就在自己的工作目录里 `git fetch && git rebase origin/main`、解决冲突、强推，再合并。合并后：
  `git -C <主目录> pull --ff-only` → 删掉工作目录（`git worktree remove …`）和分支（正在试用它的话等更新后再删）→ 告诉用户到
  网页「设置 → 开发」点「更新这台 Mac」（#156）。最好直接请用户在开发页点「上线」，这些都会自动做。不要直接 `ccim restart` 别的项目到开发版。
  `ccim try main` 会重启本机跑开发版的进程（网页平台、ccim 测试机器人），它们才用上刚合并的 Python 代码。web.html 每次请求现读，
  刷新网页就是新的；后台没重启时，新网页配旧后台，新功能会悄悄失效（#61 合并后删终端对话就这样没生效）。
  重启会打断正在进行的对话（包括正在和用户说话的这一轮），所以先告诉用户、等用户同意再跑。
- 开发页（#156，`ccim/devpanel.py`，网页「设置 → 开发」，只在跑开发版的电脑上有）：每个等验收的需求（开着的 PR）只有「试用」「上线」。
  试用 = `set_try`（显示本机地址，只在这台 Mac 上能打开）；上线（可多选）= 逐个 `_merge`（每次现问 PR 状态，落后就在工作目录 rebase、强推，
  等检查过了 squash 合并，有冲突的跳过）→ `_update`（pull → `start_promote` → `set_try("main")`），只重启一次。结果存 stable/dev-job.json，
  平台重启后还看得到。别处合并了、这台 Mac 没用上（当前稳定版之后 main 上有提交，或进程启动时的代码不是最新）时显示「更新这台 Mac」；
  后台升级还在做（`runtime.promoting`：promote-run 拿着 .promote.lock）时不算，显示「正在更新」，缓存 5 秒就重算、网页 5 秒再取一次。
  GitHub 数据后台每 2 分钟刷新，红点数（state 的 ccim.dev）读缓存；设置图标的红点再加上比服务器旧的电脑（`oldMachines`，「电脑」页给升级按钮）。
  每条链回提需求的对话：对话 meta 的 issues（`Chat._post_bash` 在 gh issue create / reopen、gh pr create 后记），
  老对话从回答里贴过的 issue 链接认（`devpanel.refs`）。任务面板（#157）：chat_view 带 issue（最近两轮里提到的最近那个，`issue_of`，
  标题 `Titles` 存平台 state 的 issue_titles，没有的后台用 gh 取），面板这一行标题换成「#编号 issue 标题」，做过好几个的标「还有 N 个」。
  **对话里不再升级**：`Chat._pre_bash` 拦下 `ccim promote`（原来 #146 是弹确认卡片）。用户在对话里说验收通过时，请用户到「设置 → 开发」
  点「上线」（合并 + 本机更新一步做完）；自己合并了的，提醒用户点「更新这台 Mac」。终端里照样能跑 promote（装依赖沿用当前稳定版的版本 `_pins`，10 分钟超时）
- 合并进 main 后 GitHub Actions（.github/workflows/deploy.yml）先用 Python 3.11 检查，再把服务器上的总控更新到这个版本。
  别的电脑在网页「设置 → 电脑」里点升级（#37）。
- 部署：Actions 用 `git archive` 打包从标准输入交给服务器，部署钥匙在服务器 authorized_keys 里限定成只能跑
  ~/ccim-hub/hub-update.sh（仓库里的 deploy/hub-update.sh，改了要手动拷上去），新版本起不来就装回上一个，版本记在 deployed.txt。
  仓库 Secrets：DEPLOY_SSH_KEY、DEPLOY_KNOWN_HOSTS、DEPLOY_HOST（用户@地址；不能放仓库变量，变量会原样打印在公开的运行日志里）。只有 lzy2025ai/ccim 会部署，fork 不会
- 推送、拉取已在仓库配置里固定用 lzy2025ai，不用切账号。gh 命令一律带上令牌：`GH_TOKEN=$(gh auth token -u lzy2025ai) gh …`。
  不要用 `gh auth switch`，它改的是全机共用的选中账号，会影响别的会话和仓库。
- 提交时只 `git add` 自己改的文件，不要 `git add -A`。
- 重启前先等（#136）：`safe_restart` 停进程前 `_before_restart`：等这个进程里的对话这一轮做完（state chats 的 busy，最多 WAIT_IDLE 180 秒），
  还没做完的记 restart_cut；在这个进程里的对话发起的（环境变量 CCIM_PROCESS 是它、CCIM_CHAT 是那个对话），记 restart_report（`ccim try` 用
  CCIM_RESTART_LABEL / CCIM_RESTART_PATHS 带上切到哪、重启了哪些）。新进程连上后 `_after_restart` → `runtime.after_restart_note`：
  等那几个进程都起来（最多 2 分钟）在发起的对话里说结果，被打断的对话里说一声（微信不发）。进程启动时把 state 里残留的 busy 清掉。
  注意：独立进程优先用稳定版跑，这部分改动要 promote 之后才生效
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
