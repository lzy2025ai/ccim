"""每个聊天对应一个 Claude 会话：排队、打断、空闲回收、按 session_id 恢复。

一个聊天一个 worker 协程，Claude 客户端的连接、提问、断开都在这个协程里做；
空闲 30 分钟就断开，下一条消息再按 session_id 接上。
"""
import asyncio, logging, os, shutil, subprocess, sys, time, uuid

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent, ResultMessage,
                              StreamEvent, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
                              create_sdk_mcp_server, tool)

from . import registry, usage
from .progress import clock, describe_tool, short

log = logging.getLogger("ccim.session")

IDLE = 30 * 60
REFRESH = 2.5               # 工作中卡片的刷新间隔（秒）
CARD_AFTER = 4              # 没有工具调用的简单问答，Claude 连上后超过这么多秒才出工作中卡片
SHOW_STEPS = 6
RESET = object()
ATTACH = object()
MAX_BUFFER = 64 * 1024 * 1024   # SDK 默认单条消息 1 MB，Claude 读图片、大文件时会超，放宽到 64 MB
STRAY_WAIT = 30           # 收到一个什么都没说的「一轮结束」（比如接上对话时补的后台任务通知那一轮）后，再等多久用户这一轮的结果
INJECT_GRACE = 4          # 插进去的消息来得太晚（最后一步之后）时，这一轮结束后等几秒看 Claude 会不会接着处理
REDELIVER_EVERY = 20       # 回答没发出去时，后台每隔多少秒补发一次（最多 10 分钟）
# 优先用本机装的 claude，和终端里的版本、登录一致；找不到再用 SDK 自带的
CLI = shutil.which("claude") or next((p for p in map(os.path.expanduser, ["~/.local/bin/claude", "~/.claude/local/claude",
                                     "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]) if os.path.exists(p)), None)

APPEND = """\
你正在通过飞书和用户对话（ccim 把这个项目目录接到了飞书）。用户多半在手机上看回复：
- 回答简洁，用飞书能显示的 Markdown（标题、加粗、列表、代码块、链接），不要用表格和 HTML。
- 需要把图片、视频、文件交给用户时，调用 mcp__ccim__send_file 工具发到聊天里。
- 用户发来的图片和文件已保存到本地，路径写在消息里，需要时用 Read 查看。
- 没有交互式提问框，有问题直接在回复里问。"""


class Caffeinate:
    """有任务在跑时阻止 Mac 睡眠，全部跑完再放开。"""

    def __init__(self):
        self.count, self.proc = 0, None

    def acquire(self):
        self.count += 1
        if self.count == 1 and self.proc is None:
            try:
                self.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                self.proc = None

    def release(self):
        self.count = max(0, self.count - 1)
        if self.count == 0 and self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)         # 收掉子进程，不然会留下僵尸进程
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


class Turn:
    def __init__(self):
        self.start = time.monotonic()
        self.ready = None                     # Claude 连上的时刻
        self.unseen = []                      # 插进来、Claude 可能还没看到的消息 [(内容, reply_to)]
        self.on_end = []                      # 这一轮结束时要做的事（收掉补充卡片上的按钮）
        self.drain = None                     # /stop 时还有 Claude 没看到的补充：None / "wait" 等它开新一轮 / "cut" 已经再打断一次
        self.steps = []
        self.text = ""                        # 最近一段写完的文字
        self.live = ""                        # 正在写的文字（流式）
        self.card_id = None
        self.shown = None

    def card(self, title, color):
        hidden = len(self.steps) - SHOW_STEPS
        lines = ([f"…前面还有 {hidden} 步"] if hidden > 0 else []) + [f"· {s}" for s in self.steps[-SHOW_STEPS:]]
        writing = self.live or self.text
        if writing and color == "blue":
            tail = " ".join(writing.split())
            lines.append("💬 " + (tail if len(tail) <= 160 else "…" + tail[-159:]))
        return {"title": f"{title} · {clock(time.monotonic() - self.start)}", "color": color,
                "body": "\n".join(lines) or "思考中…"}


class Chat:
    agent = "claude"
    SID_KEY = "session_id"                    # 状态文件里存对话编号的键；Codex 用自己的，切换助手时两边的对话互不干扰
    def __init__(self, bridge, chat_id, chat_type, fork_from=None):
        """chat_id 是对话地址（话题是「群|话题」）。fork_from：话题第一次开口时，从主对话的这个会话分出来。"""
        self.b, self.chat_id, self.chat_type = bridge, chat_id, chat_type
        saved = bridge.saved(chat_id)
        self.session_id = saved.get(self.SID_KEY)
        self.fork_from = None if self.session_id else fork_from or saved.get("fork_from")
        self.fork_at = saved.get("fork_at") if self.fork_from and not fork_from else None   # 从那个对话的哪条消息分出来
        # 没单独设置就是 None：不传给 Claude Code，让它按项目、全局设置自己取，和终端里用的一致
        self.model = saved.get("model") or bridge.entry.get("model")
        self.effort = saved.get("effort") or bridge.entry.get("effort")
        self.queue = asyncio.Queue()
        self.worker = None
        self.client = None
        self.turn = None
        self.stopping = False
        self.reconnect = False

    # ----- 对外 -----

    def submit(self, prompt, reply_to=None, message_id=None):
        """message_id 是用户那条消息：先给它加个「敲键盘」表情表示收到了，这一轮做完再去掉。"""
        react = asyncio.create_task(self.b.channel.add_reaction(message_id)) if message_id else None
        self.b.save(self.chat_id, type=self.chat_type, last_active=registry.now())
        self.queue.put_nowait((prompt, reply_to, react))
        if not self.worker or self.worker.done():
            self.worker = asyncio.create_task(self._work())
        waiting = sum(1 for item in self.queue._queue if item[0] not in (RESET, ATTACH))   # 内部信号不算排队
        return waiting - 1 + (1 if self.turn else 0)

    @property
    def busy(self):
        return self.turn is not None

    async def inject(self, prompt, reply_to=None):
        """把消息交给正在做的这一轮：Claude 下一步就会看到。这一轮没在跑就返回 False，由调用方照常排队。"""
        t = self.turn
        if not t or not t.ready or not self.client or self.stopping:
            return False
        try:
            await self.client.query(prompt)
        except Exception:
            log.warning("补充消息没交给 Claude", exc_info=True)
            return False
        t.unseen.append((prompt, reply_to))
        t.steps.append("收到补充：" + short(prompt, 40))
        return True

    async def interrupt_with(self, prompt, reply_to=None, message_id=None):
        """停掉正在做的这一轮，马上处理这条；排着队的其他消息保留。"""
        react = asyncio.create_task(self.b.channel.add_reaction(message_id)) if message_id else None
        self._push_front((prompt, reply_to, react))
        await self._interrupt()

    def _push_front(self, item):
        self.queue.put_nowait(item)
        self.queue._queue.rotate(1)           # 刚放进去的挪到最前面
        if not self.worker or self.worker.done():
            self.worker = asyncio.create_task(self._work())

    async def stop(self):
        while not self.queue.empty():
            _, _, react = self.queue.get_nowait()
            asyncio.create_task(self._unreact(react))
        return await self._interrupt()

    async def _interrupt(self):
        if not self.turn:
            return False
        self.stopping = True
        self.b.approvals.cancel_chat(self.chat_id)
        if not self.client:                   # 还在连接 Claude：_turn 连上后看到 stopping 就不提问了
            return True
        try:
            await self.client.interrupt()
        except Exception:
            log.warning("打断失败，直接断开", exc_info=True)
            if self.worker:
                self.worker.cancel()
        return True

    async def reset(self):
        """开新会话。正在跑的那一轮先停掉；断开要在 worker 里做，所以排一个重置信号进去。"""
        await self.stop()
        if self.worker and not self.worker.done():
            self.queue.put_nowait((RESET, None, None))
        else:
            self._forget()

    def _forget(self):
        self.session_id = self.fork_from = self.fork_at = None
        self.b.save(self.chat_id, fork_from=None, fork_at=None, **{self.SID_KEY: None})

    async def attach(self, session_id, at=None):
        """把这个聊天接到另一个对话（比如终端里开的）上。总是另开分支，那边的对话不受影响。
        at：只接到那个对话里的这条消息为止（桌面端转过来时，去掉还没跑完的那一轮）。"""
        await self.stop()
        if self.worker and not self.worker.done():
            self.queue.put_nowait((ATTACH, (session_id, at), None))
        else:
            self._attach(session_id, at)

    def _attach(self, session_id, at=None):
        self._forget()
        self.fork_from, self.fork_at = session_id, at
        self.b.save(self.chat_id, fork_from=session_id, fork_at=at)

    def effective(self):
        return registry.effective(self.b.path, self.model, self.effort)

    def set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.b.save(self.chat_id, **kw)
        self.reconnect = True

    # ----- worker -----

    async def _work(self):
        try:
            while True:
                try:
                    prompt, reply_to, react = await asyncio.wait_for(self.queue.get(), IDLE)
                except asyncio.TimeoutError:
                    log.info("[%s] 空闲 %d 分钟，断开会话", self.chat_id[-6:], IDLE // 60)
                    return
                if prompt is RESET:
                    await self._disconnect()
                    self._forget()
                    continue
                if prompt is ATTACH:
                    await self._disconnect()
                    self._attach(*reply_to)
                    continue
                if self.reconnect:
                    await self._disconnect()
                    self.reconnect = False
                self.stopping = False
                self.b.caffeinate.acquire()
                try:
                    await self._turn(prompt, reply_to)
                except Exception as e:
                    log.exception("[%s] 这一轮出错", self.chat_id[-6:])
                    await self._disconnect()
                    await self._safe_text(f"出错了：{short(str(e) or type(e).__name__, 300)}", reply_to)
                finally:
                    self.turn = None
                    self.b.caffeinate.release()
                    await self._unreact(react)
        finally:
            await self._disconnect()

    async def _connect(self):
        if self.client:
            return
        tries = [(self.session_id, False)] if self.session_id else [(self.fork_from, True)] if self.fork_from else []
        for resume, fork in tries + [(None, False)]:
            opts = ClaudeAgentOptions(fork_session=fork, resume_session_at=self.fork_at if fork else None,
                cwd=self.b.path, permission_mode="auto", can_use_tool=self._can_use_tool,
                setting_sources=["user", "project", "local"], model=self.model, effort=self.effort,
                resume=resume, mcp_servers={"ccim": self._tools()}, disallowed_tools=["AskUserQuestion"],
                include_partial_messages=True, max_buffer_size=MAX_BUFFER,
                system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND}, cli_path=CLI,
                stderr=lambda line: log.debug("claude: %s", line))
            client = ClaudeSDKClient(options=opts)
            try:
                await client.connect()
                self.client = client
                if self.fork_from:            # 分支开好了（或者分不出来改开了新对话），都不用再分
                    self.fork_from = self.fork_at = None
                    self.b.save(self.chat_id, fork_from=None, fork_at=None)
                self.b.save(self.chat_id, live=True)   # 给 ccim resume 判断飞书这边是不是还开着
                return
            except Exception:
                if not resume:
                    raise
                log.warning("[%s] 接不上之前的会话 %s，开新会话", self.chat_id[-6:], resume[:8], exc_info=True)
                self.session_id = None

    async def _disconnect(self):
        c, self.client = self.client, None
        if c:
            self.b.save(self.chat_id, live=False)
            try:
                await c.disconnect()
            except Exception:
                log.debug("断开出错", exc_info=True)

    async def _turn(self, prompt, reply_to):
        self.b.save(self.chat_id, busy=True)  # 桌面端同步飞书内容时，据此跳过还没答完的这一轮
        try:
            await self._turn_inner(prompt, reply_to)
        finally:
            self.b.save(self.chat_id, busy=False)
            for fn in (self.turn.on_end if self.turn else []):
                fn()

    async def _turn_inner(self, prompt, reply_to):
        t = self.turn = Turn()
        ticker = asyncio.create_task(self._tick(t, reply_to))
        result, error = None, None
        try:
            await self._connect()
            t.ready = time.monotonic()
            if not self.stopping:
                await self.client.query(prompt)
                it = self.client.receive_messages().__aiter__()
                grace = False                 # 这一轮已结束、正在等 Claude 处理来得太晚的补充
                stray = False                 # 收到的「一轮结束」不是这一轮的，接着等
                while True:
                    wait = INJECT_GRACE if grace else STRAY_WAIT if stray else None
                    try:
                        m = await (asyncio.wait_for(it.__anext__(), wait) if wait else it.__anext__())
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if grace:
                            for p, r in t.unseen:     # Claude 没接着处理：照常排到最前面
                                self._push_front((p, r, None))
                            t.unseen = []
                        break
                    if stray and not isinstance(m, RateLimitEvent):
                        stray = False
                    if isinstance(m, SystemMessage) and m.subtype == "init":
                        sid = (m.data or {}).get("session_id")
                        if sid and sid != self.session_id:  # 一开始就记下，第一轮就被打断也能接上
                            self.session_id = sid
                            self.b.save(self.chat_id, **{self.SID_KEY: sid})
                    if grace and not isinstance(m, RateLimitEvent):
                        grace, t.unseen = False, []   # Claude 接着处理补充了，读到下一轮结束
                        if t.drain == "wait":
                            t.drain = "cut"
                            try:
                                await self.client.interrupt()
                            except Exception:
                                log.warning("打断补充的那一轮失败", exc_info=True)
                    if isinstance(m, UserMessage):
                        if (isinstance(m.content, list) and any(isinstance(b, ToolResultBlock) for b in m.content)
                                and not self.stopping):    # 打断时那条「已中断」的工具结果不算
                            t.unseen = []         # 插进来的消息会跟着工具结果一起交给 Claude
                    elif isinstance(m, RateLimitEvent):
                        try:
                            usage.record(m.rate_limit_info)
                        except Exception:
                            log.debug("记录额度失败", exc_info=True)
                    elif isinstance(m, StreamEvent):
                        ev = m.event or {}
                        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                            t.live += ev["delta"].get("text", "")
                        elif ev.get("type") == "message_start":
                            t.live = ""
                    elif isinstance(m, AssistantMessage):
                        for blk in m.content:
                            if isinstance(blk, ToolUseBlock):
                                d = describe_tool(blk.name, blk.input)
                                if d:
                                    t.steps.append(d)
                            elif isinstance(blk, TextBlock) and blk.text.strip():
                                t.text, t.live = blk.text, ""
                    elif isinstance(m, ResultMessage):
                        if m.session_id and m.session_id != self.session_id:
                            self.session_id = m.session_id
                            self.b.save(self.chat_id, **{self.SID_KEY: m.session_id})
                        if m.is_error and not self.stopping:
                            error = m.result or "; ".join(m.errors or []) or m.subtype
                        else:
                            result = f"{result}\n\n{m.result}" if result and m.result else (m.result or result)
                        said = (m.result or t.text or "").strip()
                        if (not self.stopping and not m.is_error and not t.steps and not t.unseen
                                and said in ("", "No response requested.")):
                            # 接上对话时 Claude Code 可能先补一轮（被杀掉的后台任务的通知），回答为空；用户这一轮还在后面
                            result, t.text, stray = None, "", True
                            continue
                        if self.stopping and t.unseen and t.drain is None:
                            # /stop 时还有 Claude 没看到的补充：Claude Code 会自动开一轮处理它，要把那一轮也打断、读完扔掉，
                            # 不然它的回答会落到下一条消息头上
                            t.drain, t.unseen, grace = "wait", [], True
                            continue
                        if t.drain == "cut":
                            break
                        if t.unseen and not self.stopping:
                            grace = True
                            continue
                        break
        finally:
            ticker.cancel()
        await self._finish(t, result, error, reply_to)

    async def _finish(self, t, result, error, reply_to):
        """这一轮结束：先发回答，再收尾工作中卡片。Claude Code 和 Codex 共用。"""
        if self.stopping:
            title, color = "已停止", "grey"
        elif error:
            title, color = "出错", "red"
        else:
            title, color = "完成", "green"
        # 先发回答再收尾卡片：卡片接口卡住时不能拖住回答
        answer = None if error or self.stopping else result or t.text
        if error:
            await self._safe_text(f"出错了：{short(error, 500)}", reply_to)
        elif self.stopping and not t.card_id:
            await self._safe_text("已停止。", reply_to)
        elif answer:
            await self._deliver(answer, reply_to)
        if t.card_id and (t.steps or self.stopping or error):
            await self._safe_update(t.card_id, t.card(title, color))
        elif t.card_id:                       # 纯文字回答：卡片收成一行小字（撤回的话飞书会留「撤回了一条消息」，像出了错）
            await self._safe_update(t.card_id, {"note": f"已回复 · 用时 {clock(time.monotonic() - t.start)}"})
        if answer:
            log.info("→ 回复 %d 字，用时 %.1f 秒（启动会话 %.1f 秒）%s", len(answer or ""), time.monotonic() - t.start,
                     (t.ready or t.start) - t.start, f"，{len(t.steps)} 步" if t.steps else "")

    async def _tick(self, t, reply_to):
        while True:
            await asyncio.sleep(0.5 if not t.card_id else REFRESH)
            if not t.card_id:
                if not t.steps and (not t.ready or time.monotonic() - t.ready < CARD_AFTER):
                    continue
                try:
                    t.card_id = await self.b.channel.send_card(self.chat_id, t.card("工作中", "blue"), reply_to)
                except Exception:
                    log.warning("发送工作中卡片失败（%s）", short(str(sys.exc_info()[1]), 120))
                    return
                continue
            await self._safe_update(t.card_id, t.card("工作中", "blue"))

    async def _deliver(self, answer, reply_to):
        """发最终回答。几次重试都失败（比如断网）就转到后台，每 20 秒再试，最多 10 分钟；
        每次用同一个 uid，之前其实已经发出去的不会重复。"""
        uid = uuid.uuid4().hex
        try:
            await self.b.channel.send_text(self.chat_id, answer, reply_to, uid=uid)
            return
        except Exception as e:
            log.warning("回答没发出去（%s），转到后台继续重发", e)

        async def retry():
            for _ in range(600 // REDELIVER_EVERY):
                await asyncio.sleep(REDELIVER_EVERY)
                try:
                    await self.b.channel.send_text(self.chat_id, answer, reply_to, uid=uid)
                    log.info("之前没发出去的回答已补发")
                    return
                except Exception as e:
                    log.warning("补发回答失败（%s）", e)
            log.error("回答 10 分钟都没发出去，放弃：%s", short(answer, 80))
        task = asyncio.create_task(retry())
        self.b.background.add(task)
        task.add_done_callback(self.b.background.discard)

    async def _unreact(self, react):
        if not react:
            return
        try:
            rid = await react
            if rid:
                await self.b.channel.remove_reaction(*rid)
        except Exception:
            log.debug("去掉表情失败", exc_info=True)

    async def _safe_update(self, mid, card):
        try:
            await self.b.channel.update_card(mid, card)
        except Exception:
            log.warning("更新卡片失败（%s）", short(str(sys.exc_info()[1]), 120))

    async def _safe_text(self, text, reply_to=None):
        try:
            await self.b.channel.send_text(self.chat_id, text, reply_to)
        except Exception:
            log.warning("发送消息失败（%s）", short(str(sys.exc_info()[1]), 120))

    # ----- 权限、工具 -----

    async def _can_use_tool(self, name, inp, ctx):
        return await self.b.approvals.ask(self.chat_id, name, inp, ctx)

    def _tools(self):
        chat = self

        @tool("send_file", "把本机的图片、视频或文件发到当前飞书聊天里。path 用绝对路径，或相对项目目录的路径。",
              {"path": str})
        async def send_file(args):
            path = os.path.realpath(os.path.join(chat.b.path, os.path.expanduser(args.get("path", ""))))
            if chat.chat_type != "p2p" and not path.startswith(os.path.realpath(chat.b.path) + os.sep):
                return _err("群聊里只能发送项目目录里的文件。")
            if not os.path.isfile(path):
                return _err(f"文件不存在：{path}")
            try:
                await chat.b.channel.send_file(chat.chat_id, path)
            except Exception as e:
                return _err(f"发送失败：{e}")
            return {"content": [{"type": "text", "text": f"已发送 {os.path.basename(path)}"}]}

        return create_sdk_mcp_server("ccim", tools=[send_file])


def _err(text):
    return {"content": [{"type": "text", "text": text}], "is_error": True}
