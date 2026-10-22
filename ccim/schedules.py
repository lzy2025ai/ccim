"""智能体平台的定时任务（三期，方案见 docs/智能体平台-产品方案.md）。

定时任务存在平台 state.json 的 schedules 里（只在主循环里改），每次运行一条记录（web/schedule-<编号>.jsonl，run 同一个 id 会被更新）。
平台进程里一个循环每 30 秒看一次：到点了就跑，然后按频率算下一次（next_run，存盘）。电脑睡着、ccim 没在运行错过了的，
醒来发现已经过点就补跑一次（catchup），不管错过几次只补一次——下一次总是从「现在」往后算。

给 Agent 的：每次一个新网页对话 s-<任务>-<运行>，或者一直用同一个 s-<任务>（session=same，记得之前跑过的）；
这些对话 meta 里有 schedule，不进对话列表。给项目的：走项目派活（一起做，不对比），等各人都做完。
deliver 里有 feishu / weixin 的，做完用这个 Agent 接的机器人私聊主人（channels.send_once）。
给对话的（kind=chat，Agent 用 ccim schedule 在网页对话里建的，#122）：到点在那个对话里跑，接着它的上下文。
quiet：提示里让它没有值得说的就只回 QUIET，这种回答网页不记（WebChannel.send_text），对话里那条任务也去掉，也不发飞书、微信。
"""
import asyncio, datetime as dt, logging, time, uuid

from . import agents, registry

log = logging.getLogger("ccim.schedules")

TICK = 30
CATCHUP_AFTER = 300                          # 比预定时间晚这么多秒才跑，算补跑
FREQS = ("daily", "workday", "weekly", "monthly", "once", "every")
EVERY = (5, 24 * 60)                         # 每隔几分钟：最少、最多
QUIET = "[无需通知]"
QUIET_NOTE = (f"\n\n（这是定时任务到点自动发来的，用户这时不一定在看。这次没有值得告诉用户的新情况时，"
              f"只回复「{QUIET}」这几个字，不要别的；有就照常回答。）")
PROJECT_WAIT = 4 * 3600                      # 项目任务最多等这么久


def is_quiet(text):
    return (text or "").strip().strip("。.").strip() == QUIET


class Problem(Exception):
    """给用户看的错误。"""


def next_time(s, after):
    """按频率算 after 之后的下一次运行时间（本地时间），返回时间戳；只跑一次且已经过了的返回 None。"""
    if s["freq"] == "every":
        return after + s["every"] * 60
    hh, mm = (int(x) for x in s["time"].split(":"))
    base = dt.datetime.fromtimestamp(after)
    if s["freq"] == "once":
        d = dt.datetime.strptime(s["date"], "%Y-%m-%d").replace(hour=hh, minute=mm)
        return d.timestamp() if d.timestamp() > after else None
    day = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for i in range(0, 400):
        c = day + dt.timedelta(days=i)
        if c.timestamp() <= after:
            continue
        wd = c.weekday()
        if (s["freq"] == "daily" or (s["freq"] == "workday" and wd < 5) or (s["freq"] == "weekly" and wd in s["days"])
                or (s["freq"] == "monthly" and c.day == s["dom"])):
            return c.timestamp()
    return None


class Schedules:
    def __init__(self, pf):
        self.pf = pf
        self.running = {}                     # 运行编号 → 正在跑的 asyncio 任务
        self.loop_task = None

    # ----- 存取（主循环里） -----

    def all(self):
        return registry.read_state(self.pf.home).get("schedules") or {}

    def _write(self, data):
        registry.write_state(self.pf.home, schedules=data)

    def get(self, sid):
        s = self.all().get(sid)
        if not s:
            raise Problem("找不到这个定时任务。")
        return dict(s, id=sid)

    def _log(self, sid):
        return f"schedule-{sid}"

    def _emit(self, sid):
        self.pf.channel.events.publish({"type": "schedule", "schedule": sid})

    def _clean(self, f):
        try:
            return self._clean_inner(f if isinstance(f, dict) else {})
        except Problem:
            raise
        except (TypeError, ValueError, AttributeError, KeyError):
            raise Problem("填的内容有不对的地方，检查一下再保存。")

    def _clean_inner(self, f):
        for k in ("name", "prompt", "time", "date", "freq", "session"):
            if f.get(k) is not None and not isinstance(f[k], str):
                raise Problem("填的内容有不对的地方，检查一下再保存。")
        if not isinstance(f.get("target") or {}, dict) or not isinstance(f.get("days") or [], list) \
                or not isinstance(f.get("deliver") or [], list):
            raise Problem("填的内容有不对的地方，检查一下再保存。")
        name = (f.get("name") or "").strip()
        prompt = (f.get("prompt") or "").strip()
        if not name:
            raise Problem("先起个名字。")
        if not prompt:
            raise Problem("写上要它做什么。")
        t = f.get("target") or {}
        if t.get("kind") == "chat":           # 回到建任务的那个网页对话里跑
            c = self.pf.chats().get(t.get("id") or "")
            if not c or c.get("project") or c.get("schedule") or c.get("mention_of"):
                raise Problem("找不到这个对话。")
            target = {"kind": "chat", "id": t["id"]}
        elif t.get("kind") == "group":        # 群聊：不选人就是交给主 Agent
            g = self.pf.chats().get(t.get("id") or "")
            if not g or not g.get("agent"):
                raise Problem("找不到这个群聊。")
            from . import groups
            who = [a for a in (t.get("agents") or []) if a in groups.members(g)]
            target = {"kind": "group", "id": t["id"], "agents": who}
        elif t.get("kind") == "project":
            p = self.pf.projects.all().get(t.get("id"))
            if not p:
                raise Problem("找不到这个项目。")
            who = [a for a in (t.get("agents") or []) if a in (p.get("members") or [])]
            if not who:
                raise Problem("选一下项目里交给谁。")
            target = {"kind": "project", "id": t["id"], "agents": who}
        else:
            if not agents.get(t.get("id") or ""):
                raise Problem("选一下交给哪个 Agent。")
            target = {"kind": "agent", "id": t["id"]}
        freq = f.get("freq") if f.get("freq") in FREQS else "daily"
        try:
            hh, mm = (int(x) for x in (f.get("time") or "09:00").split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError):
            raise Problem("时间不对。")
        out = {"name": name, "prompt": prompt, "target": target, "freq": freq, "time": f"{hh:02d}:{mm:02d}",
               "days": sorted({int(d) for d in f.get("days") or [] if 0 <= int(d) <= 6}),
               "dom": min(max(int(f.get("dom") or 1), 1), 28), "date": f.get("date"),
               "deliver": [d for d in f.get("deliver") or [] if d in ("feishu", "weixin")],
               "session": "same" if f.get("session") == "same" else "new", "quiet": bool(f.get("quiet")),
               "every": min(max(int(f.get("every") or 60), EVERY[0]), EVERY[1])}
        if freq == "weekly" and not out["days"]:
            raise Problem("选一下每周几。")
        if freq == "once":
            try:
                dt.datetime.strptime(out["date"] or "", "%Y-%m-%d")
            except ValueError:
                raise Problem("选一下哪天跑。")
        return out

    def save(self, sid, fields):
        f = self._clean(fields)
        nxt = next_time(f, time.time())
        if f["freq"] == "once" and not nxt:
            raise Problem("这个时间已经过了。")
        data = self.all()
        if sid:
            if sid not in data:
                raise Problem("找不到这个定时任务。")
            data[sid].update(f, next_run=nxt if data[sid].get("on", True) else None)
        else:
            sid = "s" + uuid.uuid4().hex[:8]
            data[sid] = dict(f, on=True, next_run=nxt, created=registry.now())
        self._write(data)
        self._emit(sid)
        return {"id": sid}

    def toggle(self, sid, on):
        data = self.all()
        s = data.get(sid)
        if not s:
            raise Problem("找不到这个定时任务。")
        s["on"] = bool(on)
        s["next_run"] = next_time(s, time.time()) if on else None
        if on and s["freq"] == "once" and not s["next_run"]:
            raise Problem("这个时间已经过了，改个时间再恢复。")
        self._write(data)
        self._emit(sid)
        return {}

    async def delete(self, sid):
        for rid, t in list(self.running.items()):
            if rid.startswith(sid + ":"):
                raise Problem("它正在跑，等这次跑完再删。")
        for cid in [c for c, m in self.pf.chats().items() if m.get("schedule") == sid]:
            await self.pf.delete_chat(cid)
        data = self.all()
        data.pop(sid, None)
        self._write(data)
        self.pf.channel.logs.remove(self._log(sid))
        return {}

    # ----- 给网页的数据 -----

    def summary(self):
        out = []
        for sid, s in self.all().items():
            runs = [r for r in self.pf.channel.logs.read(self._log(sid)) if r.get("type") == "run"]
            last = runs[-1]["state"] if runs else None
            if last == "working" and runs[-1]["id"] not in self.running:
                last = "lost"
            out.append(dict(s, id=sid, last_state=last, running=any(r.startswith(sid + ":") for r in self.running)))
        out.sort(key=lambda r: (not r.get("on"), r.get("next_run") or 9e18))
        return out

    def detail(self, sid):
        s = self.get(sid)
        runs = [r for r in self.pf.channel.logs.read(self._log(sid)) if r.get("type") == "run"]
        for r in runs:                        # 平台重启时正在跑的：没跑完
            if r.get("state") == "working" and r["id"] not in self.running:
                r["state"] = "lost"
        return dict(s, runs=list(reversed(runs)), running=any(r.startswith(sid + ":") for r in self.running))

    # ----- 运行 -----

    def start(self):
        self.loop_task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("定时任务检查出错")
            await asyncio.sleep(TICK)

    async def _tick(self):
        now = time.time()
        data = self.all()
        due = []
        for sid, s in data.items():
            try:
                if not s.get("on") or not s.get("next_run") or s["next_run"] > now:
                    continue
                catchup = now - s["next_run"] > CATCHUP_AFTER
                s["next_run"] = next_time(s, now)      # 下一次从现在往后算：错过几次都只补这一次
                if s["freq"] == "once":
                    s["on"] = False
                if any(r.startswith(sid + ":") for r in self.running):
                    continue                            # 上一次还没跑完（比如电脑睡着时正在跑）：这次不跑了
                s["last_run"] = now
                due.append((sid, dict(s, id=sid), catchup))
            except Exception:
                log.exception("定时任务 %s 的时间算不出来，先暂停", sid)
                s["on"], s["next_run"] = False, None
        self._write(data)                              # 先存好下一次的时间，再开跑，中途出错也不会反复跑
        for sid, s, catchup in due:
            try:
                self.fire(sid, s, catchup=catchup)
            except Exception:
                log.exception("定时任务 %s 没开起来", sid)

    def run_now(self, sid):
        if any(r.startswith(sid + ":") for r in self.running):
            raise Problem("它正在跑，等这次跑完。")
        self.fire(sid, self.get(sid))
        return {}

    def fire(self, sid, s, catchup=False):
        rid = f"{sid}:{uuid.uuid4().hex[:8]}"
        rec = {"type": "run", "id": rid, "state": "working", "catchup": catchup, "at": time.time()}
        self.pf.channel.logs.append(self._log(sid), rec)
        self._emit(sid)
        t = asyncio.create_task(self._run(sid, rid, s))
        self.running[rid] = t
        t.add_done_callback(lambda _: self.running.pop(rid, None))

    async def _run(self, sid, rid, s):
        upd = {"type": "run", "id": rid}
        prompt = f"[定时任务「{s['name']}」] {s['prompt']}" + (QUIET_NOTE if s.get("quiet") and s["target"]["kind"] != "project" else "")
        try:
            if s["target"]["kind"] == "chat":
                upd.update(await self._run_chat(s["target"]["id"], s, prompt, sid))
            elif s["target"]["kind"] == "group":
                upd.update(await self._run_group(s, prompt))
            elif s["target"]["kind"] == "project":
                upd.update(await self._run_project(s, prompt))
            else:
                upd.update(await self._run_agent(sid, rid, s, prompt))
        except Exception as e:
            log.exception("定时任务 %s 出错", s.get("name"))
            upd.update(state="error", text=str(e))
        if upd.get("state") == "done" and is_quiet(upd.get("text")):
            upd.update(state="quiet", text="")
        self.pf.channel.logs.append(self._log(sid), upd)
        self._emit(sid)
        if upd.get("state") == "done" and s.get("deliver"):
            await self._deliver(s, upd.get("text") or "")

    async def _run_agent(self, sid, rid, s, prompt):
        from .session import Task
        aid = s["target"]["id"]
        # 接着同一个对话：一个 Agent 一个（改派给别的 Agent 就是另一个对话）
        cid = f"s-{sid}-{aid}" if s.get("session") == "same" else f"s-{sid}-{rid.split(':')[1]}"
        self.pf.store.deleted.discard(cid)     # 用户在对话页删过这个对话：这次重新开
        chats = self.pf.chats()
        if cid not in chats:
            chats[cid] = {"type": "p2p", "agent": aid, "schedule": sid, "title": s["name"],
                          "created": registry.now(), "last_active": registry.now()}
            self.pf.store.write(chats)
        start = time.time()
        b = self.pf._bridge(cid)
        chat = b.chat(cid, "p2p")
        self.pf.channel._emit(cid, self.pf.channel.logs.append(cid, {"type": "user", "text": prompt, "files": []}))
        task = Task(prompt)
        chat.submit(task)
        state, answer = await task.future
        state = {"ok": "done"}.get(state, state)
        last = next((r for r in reversed(self.pf.channel.logs.read(cid))
                     if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
        return {"state": state, "text": answer or last.get("text") or "", "steps": last.get("steps") or [],
                "elapsed": last.get("elapsed") or round(time.time() - start, 1), "chat": cid}

    async def _run_project(self, s, prompt):
        t = s["target"]
        start = time.time()
        r = await self.pf.projects.dispatch(t["id"], prompt, t["agents"], compare=False)
        tid = r["task"]
        while time.time() - start < PROJECT_WAIT:
            await asyncio.sleep(5)
            rs = [x for x in self.pf.channel.logs.read(f"project-{t['id']}") if x.get("type") == "reply" and x.get("task") == tid]
            if rs and all(x.get("state") not in ("queued", "working") for x in rs):
                ok = all(x.get("state") == "done" for x in rs)
                text = "\n\n".join(f"**{(agents.get(x['agent']) or {}).get('name', '')}**：{x.get('text') or ''}" for x in rs)
                return {"state": "done" if ok else "error", "text": text, "project": t["id"], "task": tid,
                        "elapsed": round(time.time() - start, 1)}
        return {"state": "error", "text": "等了太久还没做完，去项目里看看。", "project": t["id"], "task": tid}

    async def _run_group(self, s, prompt):
        """交给群聊：选了人就在群里 @ 他们，等各人做完；没选就交给主 Agent。"""
        t, start = s["target"], time.time()
        cid = t["id"]
        if t.get("agents"):
            r = await self.pf.mentions.send(cid, prompt, t["agents"])
            while time.time() - start < PROJECT_WAIT:
                await asyncio.sleep(5)
                rs = [x for x in self.pf.channel.logs.read(cid) if x.get("type") == "mention" and x.get("msg") == r["id"]]
                if rs and all(x.get("state") not in ("queued", "working") for x in rs):
                    ok = all(x.get("state") == "done" for x in rs)
                    text = "\n\n".join(f"**{self.pf.mentions._agent_name(x['agent'])}**：{x.get('text') or ''}" for x in rs)
                    return {"state": "done" if ok else "error", "text": text, "chat": cid, "elapsed": round(time.time() - start, 1)}
            return {"state": "error", "text": "等了太久还没做完，去群聊里看看。", "chat": cid}
        return await self._run_chat(cid, s, prompt)

    async def _run_chat(self, cid, s, prompt, sid=None):
        """在这个网页对话（单聊或群聊的主 Agent）里跑，接着它的上下文。对话里记一条任务（sched），不出声的那次再去掉。"""
        from .session import Task
        from . import groups
        start = time.time()
        c = self.pf.chats().get(cid)
        if not c:
            raise Problem("建这个定时任务的对话已经删掉了。")
        b = self.pf._bridge(cid)
        chat = b.chat(cid, "p2p")
        rec = self.pf.channel.logs.append(cid, {"type": "user", "id": "sched-" + uuid.uuid4().hex[:10], "text": s["prompt"],
                                                "files": [], "sched": s["name"], "sched_id": sid})
        self.pf.channel._emit(cid, rec)
        head = groups.note_prefix(self.pf, cid, groups.MAIN) + self.pf.mentions.unseen(cid) if c.get("members") else ""
        task = Task(head + prompt)
        chat.submit(task)
        state, answer = await task.future
        state = {"ok": "done"}.get(state, state)
        if state == "done" and is_quiet(answer):
            self.pf.channel.logs.drop(cid, rec["id"])
            self.pf.channel.events.publish({"type": "drop", "chat": cid, "id": rec["id"]})
            return {"state": "done", "text": QUIET, "elapsed": round(time.time() - start, 1), "chat": cid}
        last = next((r for r in reversed(self.pf.channel.logs.read(cid))
                     if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
        return {"state": state, "text": answer or last.get("text") or "", "steps": last.get("steps") or [],
                "elapsed": last.get("elapsed") or round(time.time() - start, 1), "chat": cid}

    async def _deliver(self, s, text):
        """把结果用这个 Agent 接的飞书、微信机器人私聊主人。发不出去不影响这次运行的结果。"""
        from .channels import load, send_once
        t = s["target"]
        aid = t["id"] if t["kind"] == "agent" else (self.pf.chats().get(t["id"]) or {}).get("agent") if t["kind"] in ("group", "chat") else None
        if not aid:
            return
        msg = f"【定时任务「{s['name']}」】\n\n{text}"
        for kind, proc, path in agents.linked_paths(aid):
            if kind not in s["deliver"]:
                continue
            try:
                entry = registry.get(proc)
                await asyncio.to_thread(load, entry["channel"])   # 第一次要导入飞书 SDK，好几秒，别卡住主循环
                await send_once(proc, entry, msg)
            except BaseException as e:            # 钥匙串里找不到时是 SystemExit
                log.warning("定时任务结果没发到%s：%s", kind, e)

    async def try_once(self, fields):
        """先试跑一次（不保存）：给 Agent 的开一个普通网页对话发出去，给项目的直接派到项目里。返回去哪看。"""
        f = self._clean(fields)
        prompt = f"[定时任务「{f['name']}」试跑] {f['prompt']}" + (QUIET_NOTE if f.get("quiet") and f["target"]["kind"] != "project" else "")
        if f["target"]["kind"] == "chat":
            await self.pf.send(f["target"]["id"], prompt)
            return {"chat": f["target"]["id"]}
        if f["target"]["kind"] == "group":
            if f["target"]["agents"]:
                await self.pf.mentions.send(f["target"]["id"], prompt, f["target"]["agents"])
            else:
                await self.pf.send(f["target"]["id"], prompt)
            return {"chat": f["target"]["id"]}
        if f["target"]["kind"] == "project":
            await self.pf.projects.dispatch(f["target"]["id"], prompt, f["target"]["agents"], compare=False)
            return {"project": f["target"]["id"]}
        cid = self.pf.new_chat(agent=f["target"]["id"], title=f"{f['name']}（试跑）")
        await self.pf.send(cid, prompt)
        return {"chat": cid}
