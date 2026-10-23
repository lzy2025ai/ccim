"""网页对话里 @ 其他 Agent（#11）：一条消息交给一个或几个 Agent 回答，结果放在这个对话里，方便对比。

每个被 @ 的 Agent 在这个对话里有自己的网页对话 m-<对话>-<Agent>（meta 里 mention_of，不进对话列表），
再被 @ 时接着它自己的会话，只补上它上次之后对话里新增的内容。
对话记录里：用户消息带 to；每个 Agent 一条 mention（id = <消息>:<Agent>，同一个 id 后来的覆盖前面的）。
之后不带 @ 接着聊时，原来的助手还没看过的回答拼在消息前面交给它（mention_seen 记到哪了）。
对话有工作目录时被 @ 的 Agent 在这个目录里干活，没有就在它自己的工作区。
群聊（#15）也走这里：被 @ 的不在群里就顺手拉进来；在别的电脑上的 Agent 交给那台电脑开子对话（/api/peer/task），
各人上次看到哪（ctx_upto）记在主对话的 meta 里，本机、别的电脑一样。
"""
import asyncio, os, time, uuid

from . import agents, registry

CONTEXT_LIMIT = 12000                         # 带过去的之前的内容，最多这么多字（取最近的）
SEEN_LIMIT = 6000                             # 交给原来助手的每份回答最多这么多字


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
        self.tasks = {}                       # mention id → (子对话, session.Task)
        self.waiters = {}                     # mention id → future，Agent 自己 @ 别人时等回答（ask_agents 工具）

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
                lines.append(f"{self._agent_name(r['by']) if r.get('by') else '用户'}：{r['text']}")
            elif r.get("type") == "answer" or (r.get("type") == "mention" and r.get("state") == "done"):
                lines.append(f"{self._name(m, r)}：{r['text']}")
        text = "\n\n".join(lines)
        return text[-CONTEXT_LIMIT:] if len(text) > CONTEXT_LIMIT else text

    def _ensure(self, cid, aid, cwd, title):
        sub = self.sub(cid, aid)
        self.pf.store.deleted.discard(sub)
        chats = self.pf.chats()
        if sub not in chats:
            chats[sub] = {"type": "p2p", "agent": aid, "mention_of": cid, "cwd": cwd, "title": title,
                          "created": registry.now(), "last_active": registry.now(), "seen_upto": 0}
            self.pf.store.write(chats)
        return sub

    async def send(self, cid, text, to, files=(), by=None):
        """在主循环里调。by：是对话里的 Agent 自己 @ 的（ask_agents 工具），不是用户。"""
        from . import groups
        m = self.pf.meta(cid)
        to = [a for a in dict.fromkeys(to or []) if a != m.get("agent")]
        me = self.pf.peers.me["id"]
        home = {}
        for a in to:
            home[a] = me if agents.get(a) else await asyncio.to_thread(self.pf.peers.agent_home, a)
        to = [a for a in to if home[a]]
        if not to:
            from .platform import Problem
            raise Problem("找不到要 @ 的 Agent。")
        if m.get("agent"):                    # Agent 的对话：@ 到的不在群里就顺手拉进来，这个对话就成了群聊
            groups.set_members(self.pf, cid, add=[a for a in to if a not in groups.members(m)])
        view = self.pf.chat_view(cid)
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
        for aid in to:
            sub = self.sub(cid, aid)
            prompt = groups.note_prefix(self.pf, cid, aid) + \
                (f"（下面是这个对话之前的内容，供参考。你是「{self._agent_name(aid)}」，每段开头写的是谁说的，都不是你；"
                 f"你自己在这个对话里说过的在你的记忆里，没再列出来）\n{ctx[aid]}\n\n" if ctx[aid] else "") + \
                f"[{self._agent_name(by) if by else '用户'}在对话里 @ 了你] {text}{att}"
            rid = f"{mid}:{aid}"
            self.running.add(rid)
            remote = home[aid] != me
            self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid, "msg": mid, "agent": aid,
                                                        "chat": sub, "state": "queued", **({"by": by} if by else {}),
                                                        **({"machine": home[aid]} if remote else {})}))
            if remote:                        # 在别的电脑上：交给那台电脑开子对话
                t = asyncio.create_task(self._run_remote(cid, rid, sub, prompt, home[aid], aid, title, text))
            else:
                self._ensure(cid, aid, view.get("dir"), title)
                t = asyncio.create_task(self._run(cid, rid, sub, prompt, user["at"], text))
            self.pf.background.add(t)
            t.add_done_callback(self.pf.background.discard)
            if by:
                self.waiters[rid] = asyncio.get_running_loop().create_future()
        return {"id": mid, "rids": [f"{mid}:{aid}" for aid in to]}

    async def ask(self, cid, by, text, names, timeout):
        """对话里的 Agent 自己 @ 群里的人（ask_agents 工具）：等各人回答完，返回 [(名字, 状态, 回答)]；没等到的状态是 waiting。"""
        from . import groups
        m = self.pf.meta(cid)
        ms = groups.members(m)
        if not ms:
            raise ValueError("这个对话里没有别的成员。要找谁帮忙，请用户先把那个 Agent 拉进来。")
        pick = {}
        for n in names:
            n = (n or "").strip().lstrip("@")
            hit = [a for a in ms if a == n or self._agent_name(a) == n]
            if not hit:
                raise ValueError(f"群里没有「{n}」。群成员：" + "、".join(self._agent_name(a) for a in ms))
            pick[hit[0]] = True
        r = await self.send(cid, text, list(pick), by=by)
        futs = [self.waiters[x] for x in r["rids"]]
        await asyncio.wait(futs, timeout=timeout)
        out = []
        for rid, f in zip(r["rids"], futs):
            if f.done():
                self.waiters.pop(rid, None)
                u = f.result()
                out.append((self._agent_name(rid.split(":", 1)[1]), u.get("state"), u.get("text") or ""))
            else:
                out.append((self._agent_name(rid.split(":", 1)[1]), "waiting", ""))
        return out

    def _settle(self, cid, upd):
        if cid in self.pf.chats():
            self.pf.channel._emit(cid, self.pf.channel.logs.append(cid, upd))
        f = self.waiters.get(upd["id"])
        if f and not f.done():
            f.set_result(upd)
        elif f:
            self.waiters.pop(upd["id"], None)

    async def _run(self, cid, rid, sub, prompt, upto, text):
        from .session import Task
        start = time.time()
        upd = {"type": "mention", "id": rid}
        try:
            chats = self.pf.chats()
            if sub in chats:
                chats[sub]["seen_upto"] = upto
                self.pf.store.write(chats)
            b = self.pf._bridge(sub)
            chat = b.chat(sub, "p2p")
            # 它的对话里只显示用户说的那句，带过去的之前的内容不重复显示
            self.pf.channel._emit(sub, self.pf.channel.logs.append(sub, {"type": "user", "text": text, "files": [], "from_chat": cid}))
            task = Task(prompt)
            self.tasks[rid] = (sub, task)
            chat.submit(task)
            state, answer = await task.future
            state = {"ok": "done"}.get(state, state)
            last = next((r for r in reversed(self.pf.channel.logs.read(sub))
                         if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
            upd.update(state=state, text=answer or last.get("text") or "", steps=last.get("steps") or [],
                       elapsed=last.get("elapsed"))
        except Exception as e:
            upd.update(state="error", text=str(e))
        finally:
            upd["done_at"] = time.time()
            self.running.discard(rid)
            self.tasks.pop(rid, None)
        self._settle(cid, upd)

    async def _run_remote(self, cid, rid, sub, prompt, mid, aid, title, text):
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
                                                              "kind": "mention", "of": cid, "shown": text})
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
                               elapsed=r.get("elapsed"))
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
