"""网页对话里 @ 其他 Agent（#11）：一条消息交给一个或几个 Agent 回答，结果放在这个对话里，方便对比。

每个被 @ 的 Agent 在这个对话里有自己的网页对话 m-<对话>-<Agent>（meta 里 mention_of，不进对话列表），
再被 @ 时接着它自己的会话，只补上它上次之后对话里新增的内容。
对话记录里：用户消息带 to；每个 Agent 一条 mention（id = <消息>:<Agent>，同一个 id 后来的覆盖前面的）。
之后不带 @ 接着聊时，原来的助手还没看过的回答拼在消息前面交给它（mention_seen 记到哪了）。
对话有工作目录时被 @ 的 Agent 在这个目录里干活，没有就在它自己的工作区。
群聊（#15）也走这里：被 @ 的不在群里就顺手拉进来；在别的电脑上的 Agent 交给那台电脑开子对话（/api/peer/task），
各人上次看到哪（ctx_upto）记在主对话的 meta 里，本机、别的电脑一样。
用户 @ 的 Agent 正在做这个对话交给它的活：补充进它这一轮（#304），和直接跟主 Agent 说话一样；开头写 /queue 照旧排队。
"""
import asyncio, os, re, time, uuid

from . import agents, registry

CONTEXT_LIMIT = 12000                         # 带过去的之前的内容，最多这么多字（取最近的）
SEEN_LIMIT = 6000
ASSISTANT = "assistant"                       # 记录里 by 是它：没选 Agent 的对话里的助手自己 @ 的（#179）                             # 交给原来助手的每份回答最多这么多字


def _upto(m):
    """群聊里各成员看到哪（ctx_upto，按 Agent 分）。子对话自己也能成群聊，老版本在子对话的 ctx_upto 里存的是一个时间，不算。"""
    v = m.get("ctx_upto")
    return v if isinstance(v, dict) else {}


def _seen(sub):
    """子对话上次看到主对话哪里。老版本存在 ctx_upto 里。"""
    sub = sub or {}
    v = sub.get("seen_upto", sub.get("ctx_upto"))
    return v if isinstance(v, (int, float)) else 0


class Mentions:
    def __init__(self, pf):
        self.pf = pf
        self.running = set()                  # 正在做或排着队的 mention id
        self.where = {}                       # mention id → (群聊, 成员, 子对话)：列表、任务面板里显示群里谁在干活（#192）
        self.tasks = {}                       # mention id → (子对话, session.Task)
        self.waiters = {}                     # mention id → future，对话里的助手自己 @ 别人时等回答（ask_agent 工具）
        self.joined = {}                      # mention id → [(补充那条消息, 成员, session.Task, 原话)]：补充进它这一轮的 @（#304）

    @staticmethod
    def sub(cid, aid):
        return f"m-{cid}-{aid}"

    def _agent_name(self, aid):
        a = agents.get(aid)
        if a:
            return a["name"]
        for s in self.pf.peers.cache.values():
            for x in s.get("agents") or []:
                if x.get("id") == aid:
                    return x.get("name")
        return "另一个 Agent"

    def _who(self, cid, by):
        """消息是谁发的：by 是 Agent 编号，ASSISTANT 是没选 Agent 的对话里的助手（ask_agent 工具，#179），没有就是用户。"""
        if not by:
            return "用户"
        if by == ASSISTANT:
            return self._name(self.pf.meta(cid), {})
        return self._agent_name(by)

    def _name(self, m, rec):
        if rec.get("type") == "mention":
            return self._agent_name(rec.get("agent"))
        a = agents.get(m.get("agent")) if m.get("agent") else None
        if a:
            return a["name"]
        # 没选 Agent 的对话（#177）：写明是这个对话里的哪个助手，笼统写「助手」会被当成被 @ 的人自己说的
        e = self.pf._effective(m, m)
        return f"这个对话里的 {'Codex' if e['base'] == 'codex' else 'Claude'}" + (f"（{e['model']}）" if e.get("model") else "")

    def _context(self, cid, since, aid=None):
        """对话里 since 之后的内容，整理成文字。aid 自己说过的（被 @ 时的回答、它 @ 别人的话）已经在它的会话里，不再带。"""
        m = self.pf.meta(cid)
        lines = []
        for r in self.pf.channel.logs.read(cid):
            if r.get("at", 0) <= since or not (r.get("text") or "").strip():
                continue
            if aid and (r.get("by") == aid or (r.get("type") == "mention" and r.get("agent") == aid)):
                continue
            if r.get("type") == "user":
                lines.append(f"{self._who(cid, r.get('by'))}：{r['text']}")
            elif r.get("type") == "answer" or (r.get("type") == "mention" and r.get("state") == "done"):
                lines.append(f"{self._name(m, r)}：{r['text']}")
            elif r.get("type") == "chatlog":
                lines.append(f"{self._name(m, r)}发了一份聊天记录：\n{r['text']}")
        text = "\n\n".join(lines)
        return text[-CONTEXT_LIMIT:] if len(text) > CONTEXT_LIMIT else text

    def _ensure(self, cid, aid, cwd, title, side=False):
        sub = self.sub(cid, aid)
        self.pf.store.deleted.discard(sub)
        chats = self.pf.chats()
        if sub not in chats:
            chats[sub] = {"type": "p2p", "agent": aid, "mention_of": cid, "cwd": cwd, "title": title,
                          "created": registry.now(), "last_active": registry.now(), "seen_upto": 0,
                          **({"side_at": True} if side else {})}
            self.pf.store.write(chats)
        return sub

    async def send(self, cid, text, to, files=(), by=None, depth=0):
        """在主循环里调。by：是对话里的助手自己 @ 的（ask_agent 工具），不是用户。
        depth：被 @ 的人是第几层派出来的（#179），交给它的任务带上，它再往下派时按这个拦。"""
        from . import groups
        m = self.pf.meta(cid)
        to = [a for a in dict.fromkeys(to or []) if a != m.get("agent")]
        me = self.pf.peers.me["id"]
        home = {}
        for a in to:
            home[a] = me if agents.get(a) else await asyncio.to_thread(self.pf.peers.agent_home, a)
        to = [a for a in to if home[a]]
        from .platform import Problem
        if not to:
            raise Problem("找不到要 @ 的 Agent。")
        side = m.get("side_of")               # 侧边对话里 @（#290）：被 @ 的只读，带上主对话的内容，不拉进群
        if side:
            for a in to:
                caps = (self.pf.peers.cache.get(home[a]) or {}).get("caps") or []
                if home[a] != me and "side_at" not in caps:
                    raise Problem(f"{self._agent_name(a)}所在的电脑要升级 ccim 后，才能在侧边对话里 @ 它。")
        elif m.get("agent"):                  # Agent 的对话：@ 到的不在群里就顺手拉进来，这个对话就成了群聊
            groups.set_members(self.pf, cid, add=[a for a in to if a not in groups.members(m)])
        view = self.pf.chat_view(cid)
        from .bridge import QUEUE
        head = re.match(r"(?:>[^\n]*\n)*\s*", text).end()   # 网页的引用拼在最前面，跳过它再认
        q = None if by or side else QUEUE.match(text[head:])
        plain = text[:head] + text[head + q.end():] if q else text
        mid = "u-" + uuid.uuid4().hex[:10]
        shown = [{"name": f["name"], "size": f.get("size"), "path": os.path.realpath(f["key"])} for f in files]
        # 之前的内容在记下这条消息之前取，不把它自己算进去
        upto = _upto(m)
        ctx = {aid: self._context(cid, upto.get(aid) or _seen(self.pf.chats().get(self.sub(cid, aid))), aid)
               for aid in to}
        log = self.pf.channel.logs
        user = log.append(cid, {"type": "user", "id": mid, "text": text, "files": shown, "to": to,
                                **({"by": by} if by else {})})
        self.pf.channel._emit(cid, user)
        att = "".join(f"\n（附件：{f['key']}）" for f in files)
        text = plain
        chats = self.pf.chats()
        if cid in chats:
            chats[cid]["last_active"] = registry.now()
            if not chats[cid].get("title"):
                chats[cid]["title"] = text[:30] or "新对话"
            self.pf.store.write(chats)
        title = (chats.get(cid) or m).get("title") or "新对话"
        chats = self.pf.chats()
        if cid in chats:                      # 各人看到哪了
            chats[cid]["ctx_upto"] = dict(_upto(chats[cid]), **{aid: user["at"] for aid in to})
            self.pf.store.write(chats)
        main = self._main_ctx(cid, m, to) if side else {}
        rids = []
        for aid in to:
            sub = self.sub(cid, aid)
            if side:
                prompt = main[aid] + (f"（侧边对话里之前的内容，每段开头写的是谁说的，都不是你；你自己说过的在你的记忆里）\n{ctx[aid]}\n\n"
                                      if ctx[aid] else "") + f"[用户在侧边对话里 @ 了你] {text}{att}"
            else:
                prompt = groups.note_prefix(self.pf, cid, aid) + \
                    (f"（下面是这个对话之前的内容，供参考。你是「{self._agent_name(aid)}」，每段开头写的是谁说的，都不是你；"
                     f"你自己在这个对话里说过的在你的记忆里，没再列出来）\n{ctx[aid]}\n\n" if ctx[aid] else "") + \
                    f"[{self._who(cid, by)}在对话里 @ 了你] {text}{att}"
            if not (by or side or q) and home[aid] == me:
                into = await self._inject(cid, aid, mid, prompt, text, user["at"], depth)
                if into:
                    rids.append(into)
                    continue
            rid = f"{mid}:{aid}"
            rids.append(rid)
            self.running.add(rid)
            self.where[rid] = (cid, aid, sub)
            remote = home[aid] != me
            self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid, "msg": mid, "agent": aid,
                                                        "chat": sub, "state": "queued", **({"by": by} if by else {}),
                                                        **({"machine": home[aid]} if remote else {})}))
            if remote:                        # 在别的电脑上：交给那台电脑开子对话
                t = asyncio.create_task(self._run_remote(cid, rid, sub, prompt, home[aid], aid, title, text, depth, bool(side)))
            else:
                self._ensure(cid, aid, view.get("dir"), title, bool(side))
                t = asyncio.create_task(self._run(cid, rid, sub, prompt, user["at"], text, depth))
            self.pf.background.add(t)
            t.add_done_callback(self.pf.background.discard)
            if by:
                self.waiters[rid] = asyncio.get_running_loop().create_future()
        return {"id": mid, "rids": rids}

    def _chat(self, sub):
        for b in self.pf.bridges.values():
            if sub in b.chats:
                return b.chats[sub]
        return None

    async def _inject(self, cid, aid, mid, prompt, text, upto, depth):
        """成员正在做这个对话交给它的活：这条补充进它这一轮（#304），回答合在那一条里。
        补上了返回那条 mention 的 id；它没在做（或刚好做完）返回 None，照常排队。"""
        sub = self.sub(cid, aid)
        c = self._chat(sub)
        rid = next((r for r, (s, t) in self.tasks.items() if s == sub and c and c.current and c.current[0] is t), None)
        if not rid:
            return None
        from .progress import short
        from .session import Task
        task = Task(prompt)                   # 来晚了、Claude 没接着处理时会单独排一轮，到时候按它自己的回答记
        task.depth = depth
        if not await c.inject(task):
            return None
        if c.turn and c.turn.steps:           # 进度里写用户的原话，不写拼上去的之前的内容
            c.turn.steps[-1] = "收到补充：" + short(text, 40)
        self.joined.setdefault(rid, []).append((mid, aid, task, text))
        chats = self.pf.chats()
        if sub in chats:
            chats[sub]["seen_upto"] = upto
            self.pf.store.write(chats)
        log = self.pf.channel.logs
        self.pf.channel._emit(sub, log.append(sub, {"type": "user", "text": text, "files": [], "from_chat": cid}))
        self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid, "joined": [j[0] for j in self.joined[rid]]}))
        return rid

    def _unjoin(self, cid, rid, sub):
        """那一轮做完了：补充进去、却被单独排成一轮的，改成自己的一条回答。"""
        left = self.joined.pop(rid, [])
        c = self._chat(sub)
        alone = [j for j in left if j[2].future.done() or c and (c.current and c.current[0] is j[2]
                                                                  or any(i[0] is j[2] for i in c.queue._queue))]
        if not alone:
            return
        log = self.pf.channel.logs
        self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid,
                                                    "joined": [j[0] for j in left if j not in alone]}))
        for mid, aid, task, text in alone:
            rid2 = f"{mid}:{aid}"
            self.running.add(rid2)
            self.where[rid2] = (cid, aid, sub)
            self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid2, "msg": mid, "agent": aid,
                                                        "chat": sub, "state": "queued"}))
            t = asyncio.create_task(self._run(cid, rid2, sub, None, None, text, task=task))
            self.pf.background.add(t)
            t.add_done_callback(self.pf.background.discard)

    def _main_ctx(self, cid, m, to):
        """侧边对话里 @ 人时带上的主对话内容（#290）：每人上次之后的（第一次带最近的），群聊连成员的回答一起；
        主对话正在进行的那一轮也带上。各人看到哪记在侧边对话 meta 的 main_upto。"""
        of = m["side_of"]
        title = self.pf.chat_view(of).get("title") if of in self.pf.chats() else "主对话"
        upto = dict(m.get("main_upto") or {})
        out = {}
        for aid in to:
            body = self._context(of, upto.get(aid) or 0)
            live = self.pf.live_of(of)
            head = (f"（用户在对话「{title}」旁边开了侧边对话，在那里 @ 了你。下面是主对话{'在你上次看过之后的新' if upto.get(aid) else '最近的'}内容，"
                    f"每段开头写的是谁说的；写着「{self._agent_name(aid)}」的是你在主对话里说的，你在这里不记得）\n")
            out[aid] = head + (body or "（没有新内容）") + (f"\n\n{live}" if live else "") + "\n\n" if body or live or not upto.get(aid) else ""
        rows = self.pf.channel.logs.read(of)
        at = max([r.get("at", 0) for r in rows] or [0])
        chats = self.pf.chats()
        if cid in chats:
            chats[cid]["main_upto"] = dict(upto, **{aid: at for aid in to})
            self.pf.store.write(chats)
        return out

    def report(self, cid, aid, prompt, extra):
        """侧边对话里被 @ 的 Agent 派出去的活做完了（#290）：把结果作为新的一轮交回它在侧边对话里的会话，
        由它自己在侧边栏里汇报（记录是一条 mention，report 标着，前面没有用户消息）。extra 合进这条记录（附上的文件）。在主循环里调。"""
        sub = self.sub(cid, aid)
        if cid not in self.pf.chats() or sub not in self.pf.chats():
            return
        rid = f"r-{uuid.uuid4().hex[:10]}:{aid}"
        self.running.add(rid)
        self.where[rid] = (cid, aid, sub)
        rec = self.pf.channel.logs.append(cid, {"type": "mention", "id": rid, "msg": rid.split(":")[0], "agent": aid,
                                                "chat": sub, "state": "queued", "report": True})
        self.pf.channel._emit(cid, rec)
        t = asyncio.create_task(self._run(cid, rid, sub, prompt, rec["at"], "（派出去的活有结果了）", 1, dict(extra, report=True)))
        self.pf.background.add(t)
        t.add_done_callback(self.pf.background.discard)

    def member_ctx(self, cid, m):
        """群里各成员的上下文占用（#183）：答完带回来的记在主对话 meta 的 member_ctx，没记过的看本机子对话。"""
        if not m.get("members"):
            return {}
        chats, got = self.pf.chats(), m.get("member_ctx") or {}
        out = {aid: got.get(aid) or (chats.get(self.sub(cid, aid)) or {}).get("ctx")
               for aid in m.get("members") or [] if aid != m.get("agent")}
        return {k: v for k, v in out.items() if v}

    def crew(self, cid):
        """这个群聊里被 @ 了、正在干活（或排着队）的成员：[{agent, doing}]，按开始的先后。"""
        for rid in [r for r in self.where if r not in self.running]:
            self.where.pop(rid, None)
        out, seen = [], set()
        for rid, (c, aid, sub) in self.where.items():
            if c == cid and aid not in seen:
                seen.add(aid)
                steps = (self.pf.channel.progress_of.get(sub) or {}).get("steps") or [None]
                out.append({"agent": aid, "doing": steps[-1]})
        return out

    def _settle(self, cid, upd):
        chats = self.pf.chats()
        if upd.get("ctx") and upd.get("agent") and cid in chats:
            chats[cid].setdefault("member_ctx", {})[upd["agent"]] = upd["ctx"]
            self.pf.store.write(chats)
        if cid in self.pf.chats():
            self.pf.channel._emit(cid, self.pf.channel.logs.append(cid, upd))
            if upd.get("state") == "done" and upd.get("text") and getattr(self.pf.channel, "on_answer", None):
                self.pf.channel.on_answer(cid)    # 成员答完：一句话进度按它的回答写（#192）
        f = self.waiters.get(upd["id"])
        if f and not f.done():
            f.set_result(upd)
        elif f:
            self.waiters.pop(upd["id"], None)

    async def _run(self, cid, rid, sub, prompt, upto, text, depth=0, extra=None, task=None):
        """task：已经交给它的（补充进去后被单独排成一轮的，#304），只等结果。"""
        from .session import Task
        start = time.time()
        upd = {"type": "mention", "id": rid, **(extra or {})}
        try:
            if task is None:
                chats = self.pf.chats()
                if sub in chats:
                    chats[sub]["seen_upto"] = upto
                    self.pf.store.write(chats)
                b = self.pf._bridge(sub)
                chat = b.chat(sub, "p2p")
                # 它的对话里只显示用户说的那句，带过去的之前的内容不重复显示
                self.pf.channel._emit(sub, self.pf.channel.logs.append(sub, {"type": "user", "text": text, "files": [], "from_chat": cid}))
                task = Task(prompt)
                task.depth = depth
                chat.submit(task)
            self.tasks[rid] = (sub, task)
            state, answer = await task.future
            state = {"ok": "done"}.get(state, state)
            last = next((r for r in reversed(self.pf.channel.logs.read(sub))
                         if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
            upd.update(state=state, text=answer or last.get("text") or "", steps=last.get("steps") or [],
                       said=last.get("said"), elapsed=last.get("elapsed"), agent=sub[len(f"m-{cid}-"):],
                       ctx=(self.pf.chats().get(sub) or {}).get("ctx"))
        except Exception as e:
            upd.update(state="error", text=str(e))
        finally:
            upd["done_at"] = time.time()
            self.running.discard(rid)
            self.tasks.pop(rid, None)
        self._settle(cid, upd)
        self._unjoin(cid, rid, sub)

    async def _run_remote(self, cid, rid, sub, prompt, mid, aid, title, text, depth=0, side=False):
        import json
        from .peers import Offline
        upd = {"type": "mention", "id": rid}
        self.tasks[rid] = ("remote", (mid, sub))
        hdr = {"Content-Type": "application/json"}

        async def call(path, payload):
            status, _, data = await asyncio.to_thread(self.pf.peers.forward, mid, "POST", path,
                                                      json.dumps(payload).encode(), hdr)
            return status, json.loads(data or b"{}")
        try:
            token, deadline = None, time.time() + 12 * 3600
            while token is None and rid in self.running:   # 那台电脑离线就等它上线
                try:
                    status, r = await call("/api/peer/task", {"chat": sub, "agent": aid, "prompt": prompt, "title": title,
                                                              "kind": "mention", "of": cid, "shown": text, "depth": depth,
                                                              **({"side": True} if side else {})})
                except Offline:
                    if time.time() > deadline:
                        raise RuntimeError("那台电脑一直没上线。")
                    await asyncio.sleep(15)
                    continue
                if status != 200:
                    raise RuntimeError(r.get("error") or f"出错了（{status}）")
                token = r["token"]
            if token is None:
                upd.update(state="stopped", text="")
            while token:
                await asyncio.sleep(4)
                try:
                    status, r = await call("/api/peer/result", {"token": token})
                except Offline:
                    continue
                if status != 200:
                    raise RuntimeError("那台电脑重启过，没做完。")
                if r.get("done"):
                    upd.update(state=r.get("state") or "done", text=r.get("text") or "", steps=r.get("steps") or [],
                               said=r.get("said"), elapsed=r.get("elapsed"), agent=aid, ctx=r.get("ctx"))
                    break
        except Exception as e:
            upd.update(state="error", text=str(e))
        finally:
            upd["done_at"] = time.time()
            self.running.discard(rid)
            self.tasks.pop(rid, None)
        self._settle(cid, upd)

    async def stop(self, rid):
        hit = self.tasks.get(rid, (None, None))
        if hit[0] == "remote":                # 别的电脑上的：在那边停；还在等它上线的不等了
            import json
            from .peers import Offline
            mid, sub = hit[1]
            self.running.discard(rid)
            try:
                await asyncio.to_thread(self.pf.peers.forward, mid, "POST", "/api/chat/stop",
                                        json.dumps({"id": sub}).encode(), {"Content-Type": "application/json"})
            except Offline:
                pass
            return True
        sub, task = hit
        if not task:
            return False
        for b in self.pf.bridges.values():
            c = b.chats.get(sub)
            if not c:
                continue
            waiting = [item for item in c.queue._queue if item[0] is task]
            if waiting:
                c.queue._queue.remove(waiting[0])
                task.finish("stopped")
            elif c.turn:
                await c._interrupt()
            return True
        return False

    async def stop_all(self, cid):
        hit = False
        mine = lambda r: (self.tasks[r][1][1] if self.tasks[r][0] == "remote" else self.tasks[r][0]).startswith(f"m-{cid}-")
        for rid in [r for r in list(self.tasks) if r.startswith("u-") and mine(r)]:
            hit = await self.stop(rid) or hit
        return hit

    def unseen(self, cid):
        """原来的助手还没看过的 @ 回答，拼成一段放在下一条消息前面。看过就记下。"""
        chats = self.pf.chats()
        m = chats.get(cid) or {}
        seen = m.get("mention_seen", 0)
        m_ = self.pf.meta(cid)
        rows = [r for r in self.pf.channel.logs.read(cid)
                if r.get("type") == "mention" and r.get("state") == "done" and r.get("text") and r.get("done_at", 0) > seen
                and not r.get("by")]           # 它自己 @ 的，回答已经交给它了
        if not rows:
            return ""
        if cid in chats:
            chats[cid]["mention_seen"] = time.time()
            self.pf.store.write(chats)
        parts = []
        for r in rows:
            t = r["text"]
            parts.append(f"【{self._name(m_, r)}】\n{t[:SEEN_LIMIT]}{'…' if len(t) > SEEN_LIMIT else ''}")
        return "（刚才在这个对话里 @ 了其他 Agent，它们的回答如下，供参考）\n\n" + "\n\n".join(parts) + "\n\n——\n\n"

    def lost(self, log):
        for r in log:                         # 平台重启过：没做完的标出来
            if r.get("type") == "mention" and r.get("state") == "queued" and r["id"] not in self.running:
                r.update(state="lost")
        return log

    async def delete_subs(self, cid):
        for sub in [c for c, x in self.pf.chats().items() if x.get("mention_of") == cid]:
            await self.pf.delete_chat(sub)
