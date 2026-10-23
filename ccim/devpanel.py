"""开发页（#156）：等验收的、正在体验的、已合并还没升级的需求；体验、合并、全部升级。只在跑开发版的电脑上有。

需求按 issue 列：等验收 = 开着的 PR（本机有它的工作目录才能体验）；已合并没升级 = 当前稳定版之后 main 上的提交。
每条能点回提需求的对话：对话 meta 的 issues（session._post_bash 在 Agent 建、重开 issue，开 PR 时记下）。
GitHub 上的数据用 gh 取，后台每 2 分钟刷新一次放在内存里，网页读缓存；合并、升级是后台任务，进度推送 type=dev。
"""
import json, logging, os, re, subprocess, threading, time

from . import registry, runtime

log = logging.getLogger("ccim.dev")

REFRESH = 120


def enabled():
    return runtime._is_dev_install()


def _run(cmd, timeout=60, cwd=None, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(cmd, -1, "", str(e))


def _repo():
    """GitHub 上的仓库（owner/name），从 origin 的地址认。"""
    url = runtime._git("remote", "get-url", "origin").stdout.strip()
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _gh_env(repo):
    """用仓库主人的账号（本机登了好几个 GitHub 账号时，选中的那个不一定有权限合并）。"""
    env = dict(os.environ)
    if repo:
        r = _run(["gh", "auth", "token", "-u", repo.split("/")[0]], timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            env["GH_TOKEN"] = r.stdout.strip()
    return env


def _gh(repo, args, timeout=60):
    return _run(["gh", *args, "-R", repo], timeout=timeout, env=_gh_env(repo))


def _checks(rollup):
    states = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in rollup or []]
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in states):
        return "fail"
    if any(s in ("", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED") for s in states):
        return "running"
    return "ok"


def _num(text):
    m = re.search(r"#(\d+)", text or "")
    return int(m.group(1)) if m else None


class Dev:
    def __init__(self, pf):
        self.pf = pf
        self.cache, self.at, self.lock = None, 0, threading.Lock()
        self.job = None                       # 正在做的合并 / 升级：{kind, pr, state, step, error, at}
        self.started = runtime._git("rev-parse", "HEAD").stdout.strip()   # 这个进程启动时用的代码

    # ----- 读 -----

    def overview(self, fresh=False):
        if fresh or not self.cache or time.time() - self.at > REFRESH:
            self.refresh()
        return dict(self.cache or {}, job=self.job)

    def badge(self):
        """红点上的数：等验收的 + （有没升级的就算一个）。只读缓存，不现取。"""
        c = self.cache or {}
        return len(c.get("review") or []) + (1 if c.get("merged") or c.get("stale") else 0)

    def refresh(self):
        with self.lock:
            try:
                self.cache, self.at = self._build(), time.time()
            except Exception as e:
                log.warning("开发页数据出错", exc_info=True)
                self.cache, self.at = {"error": str(e)}, time.time()

    def _build(self):
        repo = _repo()
        if not repo:
            return {"error": "这个代码目录没有连到 GitHub。"}
        runtime._git("fetch", "-q", "origin", "main")
        links = self._links(repo)
        wts = {b: p for p, b in runtime.worktrees()[1:]}
        trying = runtime.trying()
        r = _gh(repo, ["pr", "list", "--state", "open", "--limit", "50", "--json",
                       "number,title,headRefName,url,isDraft,mergeStateStatus,statusCheckRollup,closingIssuesReferences,updatedAt"])
        if r.returncode != 0:
            raise RuntimeError("读不到 GitHub 上的 PR：" + (r.stderr or r.stdout).strip()[-200:])
        review = []
        for p in json.loads(r.stdout or "[]"):
            issues = [i["number"] for i in p.get("closingIssuesReferences") or []] or [n for n in [_num(p["title"])] if n]
            n = issues[0] if issues else None
            review.append({"pr": p["number"], "url": p["url"], "issue": n, "title": runtime._change(p["title"]),
                           "branch": p["headRefName"], "local": p["headRefName"] in wts,
                           "trying": bool(trying and trying.get("branch") == p["headRefName"]),
                           "checks": _checks(p.get("statusCheckRollup")), "behind": p.get("mergeStateStatus") == "BEHIND",
                           "draft": p.get("isDraft"), "updated": p.get("updatedAt"), "chat": links.get(n)})
        cur = (runtime._channel().get("current") or "").rsplit("-", 1)[-1]
        merged = []
        if cur:
            out = runtime._git("log", "--format=%H%x09%s%x09%ct", f"{cur}..origin/main").stdout
            for line in out.splitlines():
                sha, subj, ct = (line.split("\t") + ["", ""])[:3]
                n = _num(subj)
                merged.append({"commit": sha[:7], "issue": n, "title": runtime._change(subj), "at": int(ct or 0),
                               "chat": links.get(n)})
        main = runtime._git("rev-parse", "origin/main").stdout.strip()
        return {"repo": repo, "review": review, "merged": merged, "stable": runtime._channel().get("current"),
                "last": runtime.last_promote(),
                "trying": trying and {"label": trying.get("label"), "branch": trying.get("branch")},
                "stale": bool(main and self.started and not main.startswith(self.started[:7]) and not trying),
                "at": time.time()}

    def _links(self, repo):
        """issue 编号 → 最近在做它的对话（网页对话）。先看对话里记下的（建、重开 issue 时）；
        这个功能之前的对话，从回答里贴过的 issue 链接认。"""
        out, seen = {}, {}
        pat = re.compile(r"github\.com/" + re.escape(repo) + r"/issues/(\d+)")
        logs = getattr(self.pf.channel, "logs", None)
        for cid, m in self.pf.chats().items():
            if m.get("mention_of") or m.get("helper"):
                continue
            row = {"id": cid, "title": m.get("title") or "", "agent": m.get("agent")}
            for x in m.get("issues") or []:
                n = x.get("n") if x.get("kind") == "issue" and x.get("repo") == repo else None
                if n and x.get("at", 0) >= (out.get(n) or {}).get("at", 0):
                    out[n] = dict(row, at=x.get("at", 0))
            if logs:
                for n, at in self._mentioned(logs, cid, pat).items():
                    if at >= (seen.get(n) or {}).get("at", 0):
                        seen[n] = dict(row, at=at)
        return {**seen, **out}

    _scan = {}

    def _mentioned(self, logs, cid, pat):
        """这个对话的回答里贴过哪些 issue 链接：编号 → 最后一次的时间。按文件大小和修改时间缓存。"""
        f = logs._f(cid)
        try:
            st = os.stat(f)
        except OSError:
            return {}
        hit = self._scan.get(cid)
        if hit and hit[0] == (st.st_size, st.st_mtime):
            return hit[1]
        found = {}
        with open(f, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if '"answer"' not in line or "/issues/" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                for n in pat.findall(r.get("text") or ""):
                    found[int(n)] = max(found.get(int(n), 0), r.get("at") or 0)
        self._scan[cid] = ((st.st_size, st.st_mtime), found)
        return found

    # ----- 做 -----

    def _publish(self):
        try:
            self.pf.channel.events.publish({"type": "dev", "chat": None})
        except Exception:
            pass

    def _start(self, kind, fn, **extra):
        if self.job and self.job.get("state") == "running":
            raise ValueError("上一件还在做，做完再来。")
        self.job = {"kind": kind, "state": "running", "step": "", "error": None, "at": time.time(), **extra}
        self._publish()

        def go():
            try:
                note = fn()
                self.job.update(state="done", step="", note=note)
            except Exception as e:
                log.warning("开发页 %s 出错", kind, exc_info=True)
                self.job.update(state="failed", error=str(e))
            self.job["at"] = time.time()
            self.refresh()
            self._publish()
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True}

    def _step(self, text):
        self.job["step"] = text
        self._publish()

    def try_(self, what):
        """体验某个需求（PR 编号对应的分支），或切回正式版本（main）。会重启本机跑开发版的进程，网页平台也在里面。"""
        if what != "main":
            p = next((x for x in (self.cache or {}).get("review") or [] if str(x["pr"]) == str(what)), None)
            if not p or not p["local"]:
                raise ValueError("这台电脑上没有这个需求的工作目录。")
            what = p["branch"]
        label, results = runtime.set_try(what)
        self.refresh()
        return {"label": label, "results": results}

    def merge(self, pr):
        p = next((x for x in (self.cache or {}).get("review") or [] if x["pr"] == int(pr)), None)
        if not p:
            raise ValueError("找不到这个 PR，刷新一下再试。")
        return self._start("merge", lambda: self._merge(p), pr=p["pr"], issue=p["issue"])

    def _merge(self, p):
        repo, wt = _repo(), dict((b, x) for x, b in runtime.worktrees()[1:]).get(p["branch"])
        if p["behind"] or p["checks"] == "fail":
            if not wt:
                raise RuntimeError("落后于最新代码，这台电脑上又没有它的工作目录，没法自动同步。")
            self._step("同步最新代码")
            _run(["git", "-C", wt, "fetch", "-q", "origin"], timeout=60)
            r = _run(["git", "-C", wt, "rebase", "-q", "origin/main"], timeout=120)
            if r.returncode != 0:
                _run(["git", "-C", wt, "rebase", "--abort"], timeout=30)
                raise RuntimeError("和最新代码有冲突，没法自动同步。回到提这个需求的对话，让 Agent 处理冲突。")
            r = _run(["git", "-C", wt, "push", "-q", "-f"], timeout=120)
            if r.returncode != 0:
                raise RuntimeError("推送失败：" + (r.stderr or "").strip()[-200:])
            time.sleep(10)
        self._step("等检查通过")
        deadline = time.time() + 600
        while True:
            r = _gh(repo, ["pr", "view", str(p["pr"]), "--json", "statusCheckRollup,mergeStateStatus"])
            info = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
            st = _checks(info.get("statusCheckRollup"))
            if st == "fail":
                raise RuntimeError("检查没通过，回到提这个需求的对话让 Agent 看看。")
            if st == "ok" and info.get("mergeStateStatus") not in ("BEHIND", "UNKNOWN"):
                break
            if time.time() > deadline:
                raise RuntimeError("检查 10 分钟还没跑完，稍后再试。")
            time.sleep(10)
        self._step("合并")
        r = _gh(repo, ["pr", "merge", str(p["pr"]), "--squash", "--delete-branch"], timeout=120)
        if r.returncode != 0 and "already merged" not in (r.stderr or ""):
            raise RuntimeError("合并失败：" + (r.stderr or r.stdout).strip()[-200:])
        self._step("更新本机代码")
        main = runtime._repo()
        _run(["git", "-C", main, "pull", "-q", "--ff-only"], timeout=120)
        trying = runtime.trying()
        if wt and not (trying and trying.get("branch") == p["branch"]):   # 正在体验它的：工作目录还在用，升级时切回 main 后再清
            _run(["git", "-C", main, "worktree", "remove", wt], timeout=60)
            _run(["git", "-C", main, "branch", "-D", p["branch"]], timeout=30)
        return f"已合并 #{p['issue'] or p['pr']}"

    def upgrade(self):
        return self._start("upgrade", self._upgrade)

    def _upgrade(self):
        main = runtime._repo()
        self._step("更新本机代码")
        _run(["git", "-C", main, "pull", "-q", "--ff-only"], timeout=120)
        trying = runtime.trying()
        self._step("升级稳定版")
        runtime.start_promote()               # 独立进程：装新版本、等各项目手头的活做完再重启
        if trying:                            # 切回 main 后，体验用的工作目录（已合并的）可以清掉
            merged = trying.get("branch")
            r = _run(["git", "-C", main, "branch", "-r", "--list", f"origin/{merged}"], timeout=30)
            if not r.stdout.strip():          # 分支已经合并删掉了：等平台重启好（不再用这个目录）再清，平台重启也不影响
                subprocess.Popen(["sh", "-c", 'sleep 300; git -C "$1" worktree remove "$2" && git -C "$1" branch -D "$3"', "sh",
                                  main, trying["src"], merged], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
        self._step("重启本机网页平台")
        runtime.set_try("main")               # 本机跑开发版的进程（网页平台也是）用上最新代码；平台会重启，网页断开一会儿
        return "已开始升级"
