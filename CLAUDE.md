# ccim

在飞书里和本机的 Claude Code 对话。用法见 README.md；这份文件写给改代码的人（和 Claude）。

## 结构

各模块的设计、取舍和踩过的坑在 `docs/模块/`，**改哪块代码之前先读对应的文档**；改了行为顺手更新它（只写「为什么」和坑，代码里一查就知道的不写）。

| 文档 | 管什么 |
|---|---|
| 会话.md | `session.py` 一个对话一个 Claude 会话（worker、中途补充、/stop、卡住提醒、后台任务那一轮）；`codex.py`；ccim 工具经 `toolsock.py` / `mcpstdio.py` 给任意底座；审批、斜杠命令、模型列表、/usage |
| 渠道.md | `cli.py` 主循环；`bridge.py` 消息分流；`registry.py` 登记表和状态；`daemon.py` 后台和常驻；`channels/` 飞书、微信；`weixin_hub.py` 一个微信号管多个项目；ccim-setup skill |
| 转接与派任务.md | `handoff.py` / `hook.py` 终端和飞书之间转接、网页对话转到微信 / 飞书；`asks.py` 的 `ccim ask` |
| 平台.md | 智能体平台（`ccim web`）：`agents.py`、`platform.py`、`web.py`、`web.html`、`history.py`；对话列表、分叉、退回、章节、归档、侧边对话、任务面板 `board.py`、管理员权限 `admin.py` |
| 协作.md | 群聊 `groups.py`、@ 其他 Agent `mentions.py`（含 ask_agent）、老的项目 `projects.py`、搜对话 `search.py`、对话之间转交 send_to_chat |
| 定时任务.md | `schedules.py` 和 `ccim schedule` |
| 知识库与Skill.md | `kb.py`、`skills.py`（平台 Skill、Agent 本地 Skill、Skill 助手、市场） |
| 文档与文件.md | `docs.py` 文档预览和公众号排版、`workspace.py` 工作区、`filesend.py`、`devices.py` 设备管理 |
| 多台电脑与账号.md | `peers.py` 多台电脑、`hubserver.py` 服务器总控、`share.py` 账号与共享 |
| 运行与升级.md | `runtime.py` 稳定版 / 开发版、`upgrade.py`、`onboard.py` + `join.sh` 新电脑接入 |
| 开发页与重启.md | `devpanel.py` 开发页（试用、上线）、重启前先等、部署 |

## 设计取舍

- **权限用 auto 模式**。实测过：auto 模式下普通操作（包括 rm）直接放行，auto 判定要拦的直接拒绝，都不走 `can_use_tool`；
  只有少数必须由人拍板的操作才会弹审批卡片。
- **模型和思考深度默认不传**，交给 Claude Code 按项目、全局设置自己取，这样飞书和终端里打开同一个对话用的是同一套。
  思考深度不记在对话记录里，是每次启动会话时传的，写死默认值会让两边不一致。
  用户自己设了才传，优先级：聊天里 /model /effort（state 的 chats）> 项目 `ccim config`（登记表）> `ccim config --global`
  （~/.ccim/config.json，只管 Claude Code 项目，Codex 的模型名和档位不一样）> 不传。`Chat.default` / `Chat.follow`（/model 默认）

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
- 开发页（网页「设置 → 开发」）、重启、部署的实现细节见 docs/模块/开发页与重启.md。
  **对话里不升级**（`ccim promote` 会被拦下）：用户在对话里说验收通过时，请用户到「设置 → 开发」点「上线」；自己合并了的，提醒用户点「更新这台 Mac」。
- **用户让 Agent 直接上线（不去开发页点）**：先调 /api/dev/check 看哪些能合，有合不了的先告诉用户；都能合再调 /api/dev/ship
  （和开发页的「上线」一样：按顺序合、中途失败不更新、全部合好只重启一次）。互相独立的需求各自基于 main 开分支，不要叠
- 合并进 main 后 GitHub Actions（.github/workflows/deploy.yml）先用 Python 3.11 检查，再把服务器上的总控更新到这个版本。
  别的电脑在网页「设置 → 电脑」里点升级（#37）。
- 推送、拉取已在仓库配置里固定用 lzy2025ai，不用切账号。gh 命令一律带上令牌：`GH_TOKEN=$(gh auth token -u lzy2025ai) gh …`。
  不要用 `gh auth switch`，它改的是全机共用的选中账号，会影响别的会话和仓库。
- 提交时只 `git add` 自己改的文件，不要 `git add -A`。
- 重启前先等（#136）：重启交给独立进程，停进程前会等对话这一轮做完；在 ccim 进程里跑 `ccim restart` 会自动改成独立进程。

## 约定

- **安全问题的细节不进仓库**：代码注释、CLAUDE.md、提交说明、issue、PR 都是公开的，只写中性的改动说明（做了什么），
  不写「不改的话能怎样被利用」。细节和已知风险记在仓库外的本地记录里，产品化前逐条评估

- 要能在 Python 3.11 上跑（pyproject 写的 >=3.11，别的电脑可能就是 3.11）：f-string 的 {} 里不能有反斜杠、不能用和外层相同的引号。
  改完用 3.11 过一遍：`for f in ccim/*.py ccim/channels/*.py; do python3.11 -c "import ast;ast.parse(open('$f').read())"; done`

- 卡片、回复、命令行输出里只写用户需要的信息，不写实现细节。
- 本地验证不需要飞书：写个假渠道（实现 send_text / send_card / update_card / delete_message / add_reaction / send_file / download），
  在临时目录建项目，把 `registry.HOME` 指到临时目录，直接调 `Bridge.on_message`，就能跑真实的 Claude 会话。
