"""网页对话里 @ 其他 Agent（#11）：一条消息交给一个或几个 Agent 回答，结果放在这个对话里，方便对比。

每个被 @ 的 Agent 在这个对话里有自己的网页对话 m-<对话>-<Agent>（meta 里 mention_of，不进对话列表），
再被 @ 时接着它自己的会话，只补上它上次之后对话里新增的内容。
对话记录里：用户消息带 to；每个 Agent 一条 mention（id = <消息>:<Agent>，同一个 id 后来的覆盖前面的）。
之后不带 @ 接着聊时，原来的助手还没看过的回答拼在消息前面交给它（mention_seen 记到哪了）。
对话有工作目录时被 @ 的 Agent 在这个目录里干活，没有就在它自己的工作区。
"""
import asyncio, os, time, uuid

from . import agents, registry

CONTEXT_LIMIT = 12000                         # 带过去的之前的内容，最多这么多字（取最近的）
SEEN_LIMIT = 6000                             # 交给原来助手的每份回答最多这么多字


class Mentions:
    def __init__(self, pf):
        self.pf = pf
        self.running = set()                  # 正在做或排着队的 mention id
        self.tasks = {}                       # mention id → (子对话, session.Task)

    @staticmethod
    def sub(cid, aid):
        return f"m-{cid}-{aid}"

    def _name(self, m, rec):
        if rec.get("type") == "mention":
            return (agents.get(rec.get("agent")) or {}).get("name") or "另一个 Agent"
        a = agents.get(m.get("agent")) if m.get("agent") else None
        return a["name"] if a else "助手"

    def _context(self, cid, since):
        """对话里 since 之后的内容，整理成文字。"""
        m = self.pf.meta(cid)
        lines = []
        for r in self.pf.channel.logs.read(cid):
            if r.get("at", 0) <= since or not (r.get("text") or "").strip():
                continue
            if r.get("type") == "user":
                lines.append(f"用户：{r['text']}")
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
                          "created": registry.now(), "last_active": registry.now(), "ctx_upto": 0}
            self.pf.store.write(chats)
        return sub

    async def send(self, cid, text, to, files=()):
        """在主循环里调。"""
        m = self.pf.meta(cid)
        to = [a for a in dict.fromkeys(to or []) if agents.get(a)]
        if not to:
            from .platform import Problem
            raise Problem("找不到要 @ 的 Agent。")
        view = self.pf.chat_view(cid)
        mid = "u-" + uuid.uuid4().hex[:10]
        shown = [{"name": f["name"], "size": f.get("size"), "path": os.path.realpath(f["key"])} for f in files]
        # 之前的内容在记下这条消息之前取，不把它自己算进去
        ctx = {aid: self._context(cid, (self.pf.chats().get(self.sub(cid, aid)) or {}).get("ctx_upto", 0)) for aid in to}
        log = self.pf.channel.logs
        user = log.append(cid, {"type": "user", "id": mid, "text": text, "files": shown, "to": to})
        self.pf.channel._emit(cid, user)
        att = "".join(f"\n（附件：{f['key']}）" for f in files)
        chats = self.pf.chats()
        if cid in chats:
            chats[cid]["last_active"] = registry.now()
            if not chats[cid].get("title"):
                chats[cid]["title"] = text[:30] or "新对话"
            self.pf.store.write(chats)
        title = (chats.get(cid) or m).get("title") or "新对话"
        for aid in to:
            sub = self._ensure(cid, aid, view.get("dir"), title)
            prompt = (f"（下面是这个对话之前的内容，供参考）\n{ctx[aid]}\n\n" if ctx[aid] else "") + \
                f"[用户在对话里 @ 了你] {text}{att}"
            rid = f"{mid}:{aid}"
            self.running.add(rid)
            self.pf.channel._emit(cid, log.append(cid, {"type": "mention", "id": rid, "msg": mid, "agent": aid,
                                                        "chat": sub, "state": "queued"}))
            t = asyncio.create_task(self._run(cid, rid, sub, prompt, user["at"], text))
            self.pf.background.add(t)
            t.add_done_callback(self.pf.background.discard)
        return {"id": mid}

    async def _run(self, cid, rid, sub, prompt, upto, text):
        from .session import Task
        start = time.time()
        upd = {"type": "mention", "id": rid}
        try:
            chats = self.pf.chats()
            if sub in chats:
                chats[sub]["ctx_upto"] = upto
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
        if cid in self.pf.chats():
            self.pf.channel._emit(cid, self.pf.channel.logs.append(cid, upd))

    async def stop(self, rid):
        sub, task = self.tasks.get(rid, (None, None))
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
        for rid in [r for r in list(self.tasks) if r.startswith("u-") and self.tasks[r][0].startswith(f"m-{cid}-")]:
            hit = await self.stop(rid) or hit
        return hit

    def unseen(self, cid):
        """原来的助手还没看过的 @ 回答，拼成一段放在下一条消息前面。看过就记下。"""
        chats = self.pf.chats()
        m = chats.get(cid) or {}
        seen = m.get("mention_seen", 0)
        m_ = self.pf.meta(cid)
        rows = [r for r in self.pf.channel.logs.read(cid)
                if r.get("type") == "mention" and r.get("state") == "done" and r.get("text") and r.get("done_at", 0) > seen]
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
