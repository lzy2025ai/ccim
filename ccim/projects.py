"""智能体平台的项目（群聊）。方案见 docs/智能体平台-产品方案.md 二期。

项目 = 名字、工作目录（可以没有）、成员（Agent 编号）。存在平台 state.json 的 projects 里，只在主循环里改。
项目页的消息流是一份记录（web/project-<编号>.jsonl）：task（派的活）和 reply（每个 Agent 的结果，同一个 id 会被更新）。

每个 Agent 在每个项目里有自己的对话（网页对话 p-<项目>-<Agent>，meta 里 project / cwd），派给它的活按顺序进这个对话，
所以 Agent 看不到彼此的消息；「单独聊」打开的就是这个对话。项目有工作目录时 Agent 在项目目录干活，没有就在它自己的工作区。

对比（同一件事派给几个 Agent、各做一份）：项目目录是 git 仓库时，每个 Agent 一份 git worktree（分支 ccim/<任务>-<Agent>，
目录 ~/.ccim/worktrees/<项目>/），在新对话里做，做完把改动提交到分支上；「用这个」= 把那个分支 squash 合并进项目目录当前分支，
记成一次提交。项目目录有没提交的改动时不合并。不是 git 仓库的就一起在项目目录里做。
"""
import asyncio, os, re, subprocess, time, uuid

from . import agents, registry

DIFF_LIMIT = 300 * 1024
GIT_TIMEOUT = 120                            # 仓库的提交前检查、签名密码提示可能卡住：最多等这么久


class Problem(Exception):
    """给用户看的错误。"""


def _git(d, *args, check=True):
    try:
        r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Problem("git 等了太久没反应（可能在等密码或提交前检查）。")
    if check and r.returncode != 0:
        raise Problem((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git 出错了")
    return r


def is_git(d):
    return bool(d) and os.path.isdir(d) and _git(d, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


class Projects:
    def __init__(self, pf):
        self.pf = pf
        self.running = set()                  # 正在做或排着队的 reply（平台重启后不在这里的，就是没做完的）
        self.tasks = {}                       # reply → (对话, 它的 session.Task)，停下某一件时用

    # ----- 存取（只在主循环里调） -----

    def all(self):
        return registry.read_state(self.pf.home).get("projects") or {}

    def _write(self, data):
        registry.write_state(self.pf.home, projects=data)

    def get(self, pid):
        p = self.all().get(pid)
        if not p:
            raise Problem("找不到这个项目。")
        return dict(p, id=pid)

    def _log(self, pid):
        return f"project-{pid}"

    def _emit(self, pid, rec=None):
        self.pf.channel.events.publish({"type": "project", "project": pid, **({"rec": rec} if rec else {})})

    def save(self, pid, name, dir=None, members=None):
        name = (name or "").strip()
        if not name:
            raise Problem("先给项目起个名字。")
        d = (dir or "").strip()
        if d:
            d = os.path.realpath(os.path.expanduser(d))
            if not os.path.isdir(d):
                raise Problem("找不到这个工作目录。")
        members = [m for m in (members or []) if agents.get(m)]
        data = self.all()
        if pid:
            if pid not in data:
                raise Problem("找不到这个项目。")
            data[pid].update(name=name, dir=d or None, members=members)
        else:
            pid = "p" + uuid.uuid4().hex[:8]
            data[pid] = {"name": name, "dir": d or None, "members": members, "created": registry.now(),
                         "last_active": registry.now()}
        self._write(data)
        self._emit(pid)
        return {"id": pid}

    async def stop(self, pid, tid, aid):
        """停下某一件：排着队的从队里拿掉；正在做的只打断这一件，后面排着的照常做。"""
        cid, task = self.tasks.get(f"{tid}:{aid}", (None, None))
        if not task:
            return {"stopped": False}
        for b in self.pf.bridges.values():
            c = b.chats.get(cid)
            if not c:
                continue
            waiting = [item for item in c.queue._queue if item[0] is task]
            if waiting:
                c.queue._queue.remove(waiting[0])
                task.finish("stopped")
            elif c.turn:
                await c._interrupt()
            return {"stopped": True}
        return {"stopped": False}

    async def delete(self, pid):
        if any(r.split(":")[0] in self._tasks(pid) for r in self.running):
            raise Problem("还有任务在做，先停下再删。")
        p = self.get(pid)
        for cid in [c for c in self.pf.chats() if c.startswith(f"p-{pid}-")]:   # 各 Agent 在这个项目里的对话
            await self.pf.delete_chat(cid)
        for r in self.pf.channel.logs.read(self._log(pid)):                      # 对比用的副本目录（分支留在仓库里）
            if r.get("type") == "reply" and r.get("branch"):
                wt = os.path.join(registry.HOME, "worktrees", pid, f"{r['task']}-{r['agent']}")
                await asyncio.to_thread(self._drop_tree, r.get("repo") or p.get("dir"), wt)
        for key in [k for k in self.pf.bridges if "@" in k and os.path.join(registry.HOME, "worktrees", pid) in k]:
            self.pf.bridges.pop(key, None)
        data = self.all()
        data.pop(pid, None)
        self._write(data)
        self.pf.channel.logs.remove(self._log(pid))

    def member(self, pid, add=None, remove=None):
        data = self.all()
        p = data.get(pid)
        if not p:
            raise Problem("找不到这个项目。")
        ms = list(p.get("members") or [])
        if add and add not in ms:
            if not agents.get(add):
                raise Problem("找不到这个 Agent。")
            ms.append(add)
        if remove:
            ms = [m for m in ms if m != remove]
        p["members"] = ms
        self._write(data)
        self._emit(pid)
        return {}

    def _tasks(self, pid):
        return {r["id"] for r in self.pf.channel.logs.read(self._log(pid)) if r.get("type") == "task"}

    # ----- 给网页的数据 -----

    def summary(self):
        out = []
        for pid, p in self.all().items():
            busy = any(cid.startswith(f"p-{pid}-") and c.get("busy") for cid, c in self.pf.chats().items())
            out.append({"id": pid, "name": p["name"], "dir": p.get("dir"), "members": p.get("members") or [],
                        "busy": busy, "last_active": p.get("last_active")})
        out.sort(key=lambda r: r.get("last_active") or "", reverse=True)
        return out

    def detail(self, pid):
        p = self.get(pid)
        feed = self.pf.channel.logs.read(self._log(pid))
        for r in feed:                        # 平台重启过：没做完的标出来
            if r.get("type") == "reply" and r.get("state") == "queued" and r["id"] not in self.running:
                r.update(state="lost")
        progress = {r["chat"]: self.pf.channel.progress_of.get(r["chat"]) for r in feed
                    if r.get("type") == "reply" and r.get("chat") in self.pf.channel.progress_of}
        return dict(p, feed=feed, progress=progress, git=is_git(p.get("dir")))

    # ----- 派活 -----

    async def dispatch(self, pid, text, to, compare=False, quote=None):
        p = self.get(pid)
        text = (text or "").strip()
        if not text:
            raise Problem("写上要做的事。")
        to = [a for a in dict.fromkeys(to or []) if a in (p.get("members") or [])]
        if not to:
            raise Problem("先 @ 选派给谁。")
        compare = bool(compare) and len(to) > 1
        isolate = compare and is_git(p.get("dir"))
        q = None
        if isinstance(quote, dict) and quote.get("task") and quote.get("agent"):
            r = next((x for x in self.pf.channel.logs.read(self._log(pid))
                      if x.get("type") == "reply" and x["id"] == f"{quote['task']}:{quote['agent']}"), None)
            if r and r.get("text"):
                q = {"agent": quote["agent"], "text": r["text"]}
        tid = "t" + uuid.uuid4().hex[:8]
        trees = {}
        if isolate:                           # 先把每个人的副本都开好（在线程里，git 可能慢），有一个开不了就都撤掉，不留半截
            try:
                for aid in to:
                    trees[aid] = await asyncio.to_thread(self._worktree, p, tid, aid)
            except Problem:
                for wt, branch, _ in trees.values():
                    await asyncio.to_thread(self._drop_tree, p["dir"], wt, branch)
                raise
        task = {"type": "task", "id": tid, "text": text, "to": to, "compare": compare, "quote": q}
        self._emit(pid, self.pf.channel.logs.append(self._log(pid), task))
        prompt = f"[项目「{p['name']}」派给你的任务] {text}"
        if q:
            qa = agents.get(q["agent"]) or {"name": "另一个 Agent"}
            prompt = f"（下面是「{qa['name']}」的回答，作为参考）\n{q['text']}\n\n" + prompt
        for aid in to:
            rid = f"{tid}:{aid}"
            branch = wt = None
            if isolate:
                wt, branch, cwd = trees[aid]
                cid = f"p-{pid}-{aid}-{tid}"
            else:
                cid = f"p-{pid}-{aid}"
                cwd = p.get("dir")
            self._ensure_chat(cid, aid, pid, p["name"], cwd)
            self.running.add(rid)
            rec = {"type": "reply", "id": rid, "task": tid, "agent": aid, "chat": cid, "state": "queued", "branch": branch,
                   "repo": p.get("dir") if branch else None}
            self._emit(pid, self.pf.channel.logs.append(self._log(pid), rec))
            t = asyncio.create_task(self._run(pid, rid, cid, prompt, wt, branch, p.get("dir")))
            self.pf.background.add(t)
            t.add_done_callback(self.pf.background.discard)
        data = self.all()
        data[pid]["last_active"] = registry.now()
        self._write(data)
        return {"task": tid}

    def _ensure_chat(self, cid, aid, pid, name, cwd):
        chats = self.pf.chats()
        if cid not in chats:
            chats[cid] = {"type": "p2p", "agent": aid, "project": pid, "cwd": cwd, "title": name,
                          "created": registry.now(), "last_active": registry.now()}
            self.pf.store.write(chats)

    async def _run(self, pid, rid, cid, prompt, wt, branch, pdir):
        from .session import Task
        start = time.time()
        upd = {"type": "reply", "id": rid}
        try:
            b = self.pf._bridge(cid)
            chat = b.chat(cid, "p2p")
            self.pf.channel._emit(cid, self.pf.channel.logs.append(cid, {"type": "user", "text": prompt, "files": []}))
            task = Task(prompt)
            self.tasks[rid] = (cid, task)
            chat.submit(task)
            state, answer = await task.future
            state = {"ok": "done"}.get(state, state)
            last = next((r for r in reversed(self.pf.channel.logs.read(cid))
                         if r.get("type") == "answer" and r.get("at", 0) >= start), {}) if state == "done" else {}
            upd.update(state=state, text=answer or last.get("text") or "", steps=last.get("steps") or [],
                       elapsed=last.get("elapsed"))
            if wt and state == "done":
                upd["changed"] = await asyncio.to_thread(self._commit, wt, branch, pdir, rid)
        except Exception as e:
            upd.update(state="error", text=str(e))
        finally:
            self.running.discard(rid)
            self.tasks.pop(rid, None)
        self._emit(pid, self.pf.channel.logs.append(self._log(pid), upd))

    # ----- 对比：分支和副本 -----

    def _worktree(self, p, tid, aid):
        """返回 (副本目录, 分支, Agent 在副本里的工作目录)。项目目录是仓库里的子目录时，Agent 在副本里对应的子目录干活。"""
        d = p["dir"]
        top = _git(d, "rev-parse", "--show-toplevel").stdout.strip()
        branch = f"ccim/{tid}-{aid}"
        wt = os.path.join(registry.HOME, "worktrees", p["id"], f"{tid}-{aid}")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        try:
            _git(d, "worktree", "add", "-b", branch, wt, "HEAD")
        except Problem as e:
            raise Problem(f"没能给它开一份副本：{e}")
        rel = os.path.relpath(os.path.realpath(d), os.path.realpath(top))
        return wt, branch, os.path.normpath(os.path.join(wt, rel))

    @staticmethod
    def _drop_tree(repo, wt, branch=None):
        """删掉副本目录（分支留着，「看改动」还要用）。branch 给了就连分支一起删（派活没成功时）。"""
        if repo and os.path.isdir(repo):
            _git(repo, "worktree", "remove", "--force", wt, check=False)
            if branch:
                _git(repo, "branch", "-D", branch, check=False)

    @staticmethod
    def _commit(wt, branch, pdir, rid):
        """把 Agent 在副本里的改动提交到它的分支上，返回「改了 3 个文件，+86 −41」。"""
        _git(wt, "add", "-A")
        if _git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
            _git(wt, "-c", "user.name=ccim", "-c", "user.email=ccim@localhost", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--no-verify", "-m", f"ccim 对比 {rid}")      # ccim 自己的对比分支，不跑仓库的提交前检查
        stat = _git(pdir, "diff", "--shortstat", f"HEAD...{branch}", check=False).stdout.strip()
        if not stat:
            return "没有改文件"
        n = re.search(r"(\d+) files? changed", stat)
        ins = re.search(r"(\d+) insertions?", stat)
        dels = re.search(r"(\d+) deletions?", stat)
        return f"改了 {n.group(1) if n else 0} 个文件，+{ins.group(1) if ins else 0} −{dels.group(1) if dels else 0}"

    def _reply(self, pid, tid, aid):
        r = next((x for x in self.pf.channel.logs.read(self._log(pid))
                  if x.get("type") == "reply" and x["id"] == f"{tid}:{aid}"), None)
        if not r:
            raise Problem("找不到这个结果。")
        return r

    # 下面两个在 HTTP 线程里跑（git 可能慢，不能卡住主循环）：只读状态、往记录里追加，都是线程安全的

    def diff(self, pid, tid, aid):
        p, r = self.get(pid), self._reply(pid, tid, aid)
        if not r.get("branch"):
            raise Problem("这个结果没有单独的改动可看。")
        out = _git(r.get("repo") or p["dir"], "diff", f"HEAD...{r['branch']}", check=False).stdout
        return {"diff": out[:DIFF_LIMIT], "cut": len(out) > DIFF_LIMIT}

    def choose(self, pid, tid, aid):
        p, r = self.get(pid), self._reply(pid, tid, aid)
        if not r.get("branch") or r.get("state") != "done":
            raise Problem("这个结果没有可以合并的改动。")
        if any(x.get("type") == "reply" and x.get("task") == tid and x.get("chosen") for x in self.pf.channel.logs.read(self._log(pid))):
            raise Problem("这件事已经用了一份结果了。")
        d = r.get("repo") or p["dir"]
        if not is_git(d):
            raise Problem("找不到当时的仓库了。")
        if _git(d, "status", "--porcelain").stdout.strip():
            raise Problem("项目目录里有还没提交的改动，先提交或收起，再合并。")
        a = agents.get(aid) or {"name": aid}
        task = next((x for x in self.pf.channel.logs.read(self._log(pid)) if x.get("id") == tid), {})
        m = _git(d, "merge", "--squash", r["branch"], check=False)
        if m.returncode != 0:
            _git(d, "reset", "--merge", check=False)
            raise Problem("合并时有冲突，没合进去。可以「看改动」后手动合并，或者让它基于最新代码重做。")
        if _git(d, "diff", "--cached", "--quiet", check=False).returncode != 0:
            try:
                _git(d, "commit", "-q", "-m", f"采用「{a['name']}」的改动：{(task.get('text') or '')[:60]}")
            except Problem as e:                  # 提交前检查没过、签名失败：退回合并前（合并前确认过目录是干净的）
                _git(d, "reset", "--merge", check=False)
                raise Problem(f"提交没成功，已经退回原样：{e}")
        for x in self.pf.channel.logs.read(self._log(pid)):
            if x.get("type") == "reply" and x.get("task") == tid:
                self._emit(pid, self.pf.channel.logs.append(self._log(pid), {"type": "reply", "id": x["id"], "chosen": x["id"] == r["id"]}))
        return {}
