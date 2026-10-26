# 知识库和平台 Skill

改 kb.py、skills.py 前先读。

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
  从某台电脑的 ~/.claude/skills 导入（请求发到那台电脑，装进它的平台库再同步；source 记 machine 名字和 mid 编号）。导入是拷贝，原件改了不跟着变（不做监听）：Skill 页「更新」对这种 Skill 是按 mid（老记录没有 mid 按名字找，只有一台电脑就用它）把 /api/skills/install {local} 再发到那台电脑，同名覆盖（#275）；`skills.update` 仍然只管 GitHub。Agent 分享的（source 带 agent）没有「更新」。普通 Agent 对话也有 skill_list / read / install（见 会话.md），能自己把本机 Skill 同步上来。解压时拦掉往外写的路径和软链。网页 #/skills：装好后留在列表；详情左边文件树、右边看文件（Markdown 预览 / 源码，代码用 hlCode 简单高亮，图片直接显示），能改能存（/api/skills/file/save 带打开时的 mtime，改完算这个 Skill 更新、同步）。Agent 设置里勾挂哪些
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
