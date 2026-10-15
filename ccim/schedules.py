"""智能体平台的定时任务（三期，方案见 docs/智能体平台-产品方案.md）。

定时任务存在平台 state.json 的 schedules 里（只在主循环里改），每次运行一条记录（web/schedule-<编号>.jsonl，run 同一个 id 会被更新）。
平台进程里一个循环每 30 秒看一次：到点了就跑，然后按频率算下一次（next_run，存盘）。电脑睡着、ccim 没在运行错过了的，
醒来发现已经过点就补跑一次（catchup），不管错过几次只补一次——下一次总是从「现在」往后算。

给 Agent 的：每次一个新网页对话 s-<任务>-<运行>，或者一直用同一个 s-<任务>（session=same，记得之前跑过的）；
这些对话 meta 里有 schedule，不进对话列表。给项目的：走项目派活（一起做，不对比），等各人都做完。
deliver 里有 feishu / weixin 的，做完用这个 Agent 接的机器人私聊主人（channels.send_once）。
"""
import asyncio, datetime as dt, logging, time, uuid

from . import agents, registry

log = logging.getLogger("ccim.schedules")

TICK = 30
CATCHUP_AFTER = 300                          # 比预定时间晚这么多秒才跑，算补跑
FREQS = ("daily", "workday", "weekly", "monthly", "once")
PROJECT_WAIT = 4 * 3600                      # 项目任务最多等这么久


class Problem(Exception):
    """给用户看的错误。"""


def next_time(s, after):
    """按频率算 after 之后的下一次运行时间（本地时间），返回时间戳；只跑一次且已经过了的返回 None。"""
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
        name = (f.get("name") or "").strip()
        prompt = (f.get("prompt") or "").strip()
        if not name:
            raise Problem("先起个名字。")
        if not prompt:
            raise Problem("写上要它做什么。")
        t = f.get("target") or {}
        if t.get("kind") == "project":
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
               "session": "same" if f.get("session") == "same" else "new"}
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
            out.append(dict(s, id=sid, last_state=runs[-1]["state"] if runs else None,
                            running=any(r.startswith(sid + ":") for r in self.running)))
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
        changed = False
        for sid, s in data.items():
            if not s.get("on") or not s.get("next_run") or s["next_run"] > now:
                continue
            catchup = now - s["next_run"] > CATCHUP_AFTER
            s["last_run"] = now
            s["next_run"] = next_time(s, now)
            if s["freq"] == "once":
                s["on"] = False
            changed = True
            self.fire(sid, dict(s, id=sid), catchup=catchup)
        if changed:
            self._write(data)

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
        prompt = f"[定时任务「{s['name']}」] {s['prompt']}"
        try:
            if s["target"]["kind"] == "project":
                upd.update(await self._run_project(s, prompt))
            else:
                upd.update(await self._run_agent(sid, rid, s, prompt))
        except Exception as e:
            log.exception("定时任务 %s 出错", s.get("name"))
            upd.update(state="error", text=str(e))
        self.pf.channel.logs.append(self._log(sid), upd)
        self._emit(sid)
        if upd.get("state") == "done" and s.get("deliver"):
            await self._deliver(s, upd.get("text") or "")

    async def _run_agent(self, sid, rid, s, prompt):
        from .session import Task
        aid = s["target"]["id"]
        cid = f"s-{sid}" if s.get("session") == "same" else f"s-{sid}-{rid.split(':')[1]}"
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

    async def _deliver(self, s, text):
        """把结果用这个 Agent 接的飞书、微信机器人私聊主人。发不出去不影响这次运行的结果。"""
        from .channels import send_once
        if s["target"]["kind"] != "agent":
            return
        msg = f"【定时任务「{s['name']}」】\n\n{text}"
        for kind, proc, path in agents.linked_paths(s["target"]["id"]):
            if kind not in s["deliver"]:
                continue
            try:
                await send_once(proc, registry.get(proc), msg)
            except BaseException as e:            # 钥匙串里找不到时是 SystemExit
                log.warning("定时任务结果没发到%s：%s", kind, e)

    async def try_once(self, fields):
        """先试跑一次（不保存）：给 Agent 的开一个普通网页对话发出去，给项目的直接派到项目里。返回去哪看。"""
        f = self._clean(fields)
        prompt = f"[定时任务「{f['name']}」试跑] {f['prompt']}"
        if f["target"]["kind"] == "project":
            await self.pf.projects.dispatch(f["target"]["id"], prompt, f["target"]["agents"], compare=False)
            return {"project": f["target"]["id"]}
        cid = self.pf.new_chat(agent=f["target"]["id"], title=f"{f['name']}（试跑）")
        await self.pf.send(cid, prompt)
        return {"chat": cid}
