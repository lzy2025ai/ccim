"""开发页（#156）：等验收的、正在体验的、已合并还没升级的需求；体验、合并、全部升级。只在跑开发版的电脑上有。
已上线的需求（#201）也列出来，按 issue 合并几轮 PR，能点回提需求的对话。

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


LINK = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)")
_scan = {}
PR_ISSUE = {}                                 # (仓库, PR 编号) → 它关掉的 issue（开发页刷新时记下）


def refs(logs, cid, meta, first=False):
    """这个对话在做的 issue：(仓库, 编号) → 最近一次（first：第一次）提到的时间。先看对话里记下的（Agent 建、重开 issue 时，#156）；
    这个功能之前的对话，从回答里贴过的 issue、PR 链接认。"""
    out = {}
    pick = min if first else max
    for (repo, kind, n), (a0, a1) in (_mentioned(logs, cid) if logs else {}).items():
        at = a0 if first else a1
        if kind == "pull":                    # 贴的是 PR 链接：换成它关掉的 issue
            n = PR_ISSUE.get((repo, n))
            if not n:
                continue
        out[(repo, n)] = pick(out[(repo, n)], at) if (repo, n) in out else at
    for x in meta.get("issues") or []:
        if x.get("kind") == "issue" and x.get("repo") and x.get("n"):
            k = (x["repo"], x["n"])
            out[k] = 0 if first else max(out.get(k, 0), x.get("at", 0))   # 记下的就是提需求的对话：排最前
    return out


def _mentioned(logs, cid):
    """回答里贴过的 issue、PR 链接：(仓库, issues / pull, 编号) → (第一次, 最后一次) 的时间。按文件大小和修改时间缓存。"""
    f = logs._f(cid)
    try:
        st = os.stat(f)
    except OSError:
        return {}
    hit = _scan.get(cid)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    found = {}
    with open(f, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            if '"answer"' not in line or ("/issues/" not in line and "/pull/" not in line):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            at = r.get("at") or 0
            for repo, kind, n in LINK.findall(r.get("text") or ""):
                k = (repo, kind, int(n))
                a0, a1 = found.get(k, (at, at))
                found[k] = (min(a0, at), max(a1, at))
    _scan[cid] = ((st.st_size, st.st_mtime), found)
    return found


class Titles:
    """issue 标题（任务面板用，#157）：存在平台状态里，没有的在后台用 gh 取。"""

    def __init__(self, pf):
        self.pf = pf
        self.known = dict(registry.read_state(pf.home).get("issue_titles") or {})
        self.want, self.lock, self.busy = set(), threading.Lock(), False

    def get(self, repo, n):
        k = f"{repo}#{n}"
        if k in self.known:
            return self.known[k]
        with self.lock:
            self.want.add((repo, n))
            if not self.busy:
                self.busy = True
                threading.Thread(target=self._fetch, daemon=True).start()
        return None

    def _fetch(self):
        time.sleep(1)                         # 一次列表会问到好几个，攒一下
        got = False
        while True:
            with self.lock:
                if not self.want:
                    self.busy = False
                    break
                repo, n = self.want.pop()
            r = _gh(repo, ["issue", "view", str(n), "--json", "title"], timeout=20)
            try:
                title = json.loads(r.stdout).get("title") if r.returncode == 0 else None
            except ValueError:
                title = None
            self.known[f"{repo}#{n}"] = runtime._change(title) if title else ""   # 取不到也记下，不反复问
            got = got or bool(title)
        registry.write_state(self.pf.home, issue_titles=self.known)
        if got:
            try:
                self.pf.channel.events.publish({"type": "dev", "chat": None})
            except Exception:
                pass


def issue_of(pf, cid, meta, since=0):
    """任务面板上这个对话的标题用哪个 issue：最近两轮里提到的、最近的那个，还有几个。"""
    rs = refs(getattr(pf.channel, "logs", None), cid, meta)
    if not rs:
        return None
    (repo, n), at = max(rs.items(), key=lambda kv: kv[1])
    if at < since:                            # 最近两轮已经在聊别的了
        return None
    return {"repo": repo, "n": n, "title": pf.titles.get(repo, n) or "", "more": len(rs) - 1}


def _restarting():
    """排着等重启的开发版进程（safe-restart-run 还在：等手头的对话做完、重启、看新进程起没起来）。"""
    r = _run(["ps", "-axo", "command="], timeout=10)
    paths = set(runtime.dev_projects())
    out = []
    for line in (r.stdout or "").splitlines():
        if " ccim.cli safe-restart-run " in line:
            p = line.split(" safe-restart-run ", 1)[1].split(" --", 1)[0].strip()
            if p in paths:
                out.append(p)
    return out


def _phase():
    """这台 Mac 更新到哪一步了（#156）：装新版本 → 等手头的对话做完 → 重启。做完了返回空。
    网页平台自己也在重启的进程里，所以重启做完之前一律算「正在更新」。"""
    waiting = _restarting()
    if waiting:
        busy = any(c.get("busy") for p in waiting for c in (registry.read_state(p).get("chats") or {}).values())
        return f"等手头的对话做完就重启（最多 {runtime.WAIT_IDLE // 60} 分钟）" if busy else "正在重启，网页会断开十几秒"
    return "正在装新版本" if runtime.promoting() else ""


def _num(text):
    m = re.search(r"#(\d+)", text or "")
    return int(m.group(1)) if m else None


class Dev:
    def __init__(self, pf):
        self.pf = pf
        self.cache, self.at, self.lock = None, 0, threading.Lock()
        self.job = None                       # 正在做的上线 / 更新：{kind, prs, names, state, step, note, error, at}
        self.started = runtime._git("rev-parse", "HEAD").stdout.strip()   # 这个进程启动时用的代码
        self.job = self._load_job()

    # ----- 读 -----

    def overview(self, fresh=False):
        ttl = 5 if (self.cache or {}).get("updating") else REFRESH   # 后台升级还没做完：做完马上重算（#156 第三轮）
        if fresh or not self.cache or time.time() - self.at > ttl:
            self.refresh()
        return dict(self.cache or {}, job=self.job)

    def badge(self):
        """红点上的数：等验收的 + （有没升级的就算一个）。只读缓存，不现取。"""
        c = self.cache or {}
        return len(c.get("review") or []) + (1 if (c.get("merged") or c.get("stale")) and not c.get("updating") else 0)

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
        wts = {b: p for p, b in runtime.worktrees()[1:]}
        trying = runtime.trying()
        r = _gh(repo, ["pr", "list", "--state", "open", "--limit", "50", "--json",
                       "number,title,headRefName,url,isDraft,mergeStateStatus,statusCheckRollup,closingIssuesReferences,updatedAt"])
        if r.returncode != 0:
            raise RuntimeError("读不到 GitHub 上的 PR：" + (r.stderr or r.stdout).strip()[-200:])
        prs = json.loads(r.stdout or "[]")
        for p in prs:
            issues = [i["number"] for i in p.get("closingIssuesReferences") or []] or [n for n in [_num(p["title"])] if n]
            if issues:
                PR_ISSUE[(repo, p["number"])] = issues[0]
        shipped = self._shipped(repo)
        cur = (runtime._channel().get("current") or "").rsplit("-", 1)[-1]
        log_out = runtime._git("log", "--format=%H%x09%s%x09%ct", f"{cur}..origin/main").stdout if cur else ""
        for line in log_out.splitlines():     # 合并进来的提交：「… #issue (#PR)」
            m = re.search(r"#(\d+)\D*\(#(\d+)\)\s*$", line.split("\t")[1] if "\t" in line else "")
            if m:
                PR_ISSUE[(repo, int(m.group(2)))] = int(m.group(1))
        links = self._links(repo)
        review = []
        for p in prs:
            issues = [i["number"] for i in p.get("closingIssuesReferences") or []] or [n for n in [_num(p["title"])] if n]
            n = issues[0] if issues else None
            review.append({"pr": p["number"], "url": p["url"], "issue": n, "title": runtime._change(p["title"]),
                           "branch": p["headRefName"], "local": p["headRefName"] in wts,
                           "trying": bool(trying and trying.get("branch") == p["headRefName"]),
                           "checks": _checks(p.get("statusCheckRollup")), "behind": p.get("mergeStateStatus") == "BEHIND",
                           "draft": p.get("isDraft"), "updated": p.get("updatedAt"), "chat": links.get(n)})
        merged = []
        if cur:
            for line in log_out.splitlines():
                sha, subj, ct = (line.split("\t") + ["", ""])[:3]
                n = _num(subj)
                merged.append({"commit": sha[:7], "issue": n, "title": runtime._change(subj), "at": int(ct or 0),
                               "chat": links.get(n)})
        for x in shipped:
            x["chat"] = links.get(x["issue"])
        main = runtime._git("rev-parse", "origin/main").stdout.strip()
        phase = _phase()                      # 更新做到一半时按旧版本算会说「有改动没用上」，这时不算
        updating = bool(phase)
        return {"repo": repo, "review": review, "merged": [] if updating else merged, "updating": updating, "phase": phase,
                "shipped": shipped,
                "stable": runtime._channel().get("current"),
                "last": runtime.last_promote(),
                "local_url": f"http://127.0.0.1:{registry.read_state(self.pf.home).get('port') or 8977}/",
                "trying": trying and {"label": trying.get("label"), "branch": trying.get("branch")},
                "stale": bool(main and self.started and not main.startswith(self.started[:7]) and not trying and not updating),
                "at": time.time()}

    def _shipped(self, repo):
        """已上线的需求（#201）：最近合并的 PR 按 issue 合起来，一个 issue 改过几轮只列一条（最后那次的标题和时间）。"""
        r = _gh(repo, ["pr", "list", "--state", "merged", "--limit", "200", "--json",
                       "number,title,url,mergedAt,closingIssuesReferences"])
        if r.returncode != 0:
            return []
        out = {}
        for p in sorted(json.loads(r.stdout or "[]"), key=lambda p: p.get("mergedAt") or ""):
            issues = [i["number"] for i in p.get("closingIssuesReferences") or []] or [n for n in [_num(p["title"])] if n]
            n = issues[0] if issues else None
            if n:
                PR_ISSUE[(repo, p["number"])] = n
            k = n or f"pr{p['number']}"
            rounds = out[k]["rounds"] + 1 if k in out else 1
            out[k] = {"issue": n, "pr": p["number"], "url": p["url"], "title": re.sub(r"\s*\(?#\d+\)?", "", p["title"]).strip(),
                      "merged": p.get("mergedAt"), "rounds": rounds}
        return sorted(out.values(), key=lambda x: x["merged"] or "", reverse=True)

    def _links(self, repo):
        """issue 编号 → 提这个需求的对话（网页对话）：最早提到它的那个。"""
        out = {}
        logs = getattr(self.pf.channel, "logs", None)
        for cid, m in self.pf.chats().items():
            if m.get("mention_of") or m.get("helper") or m.get("side_of"):
                continue
            for (r, n), at in refs(logs, cid, m, first=True).items():
                if r == repo and (n not in out or at < out[n]["at"]):
                    out[n] = {"id": cid, "title": m.get("title") or "", "agent": m.get("agent"), "at": at}
        return out

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
            self._save_job(self.job)
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

    def check(self, prs):
        """上线前先看一遍（#207）：哪些能合、哪些合不了（为什么），叠在一起的按什么顺序合。不改任何东西。"""
        repo, main = _repo(), runtime._repo()
        if not repo:
            raise ValueError("这个代码目录没有连到 GitHub。")
        info = {}
        for n in dict.fromkeys(int(x) for x in prs or []):
            r = _gh(repo, ["pr", "view", str(n), "--json", "number,title,state,isDraft,mergeStateStatus,statusCheckRollup,"
                                                           "headRefName,headRefOid,closingIssuesReferences"])
            if r.returncode != 0:
                raise RuntimeError(f"读不到 PR #{n}：" + (r.stderr or r.stdout).strip()[-200:])
            info[n] = json.loads(r.stdout or "{}")
        heads = [x["headRefName"] for x in info.values() if x.get("state") == "OPEN"]
        _run(["git", "-C", main, "fetch", "-q", "origin", "main", *heads], timeout=120)

        def has(a, b):                        # b 的分支里包含 a 的最新提交：b 叠在 a 后面
            return _run(["git", "-C", main, "merge-base", "--is-ancestor", info[a]["headRefOid"], info[b]["headRefOid"]],
                        timeout=30).returncode == 0
        live = [n for n, x in info.items() if x.get("state") == "OPEN"]
        below = {n: [m for m in live if m != n and has(m, n)] for n in live}
        order = sorted(live, key=lambda n: (len(below[n]), n))   # 叠得越少越先合
        items = []
        for n in order:
            x = info[n]
            issues = [i["number"] for i in x.get("closingIssuesReferences") or []] or [k for k in [_num(x["title"])] if k]
            it = {"pr": n, "issue": issues[0] if issues else None, "title": runtime._change(x["title"]),
                  "branch": x["headRefName"], "head": x["headRefOid"],
                  "after": [m for m in below[n] if not any(m in below[k] for k in below[n])],   # 直接叠在谁后面
                  "problem": None}
            if x.get("isDraft"):
                it["problem"] = "还是草稿"
            elif _checks(x.get("statusCheckRollup")) == "fail":
                it["problem"] = "检查没通过"
            elif _run(["git", "-C", main, "merge-tree", "--write-tree", "origin/main", x["headRefOid"]],
                      timeout=60).returncode != 0:
                it["problem"] = "和已经上线的改动有冲突"
            items.append(it)
        bad = {it["pr"] for it in items if it["problem"]}
        for it in items:                      # 叠在合不了的后面的，也合不了
            if not it["problem"] and any(m in bad for m in below[it["pr"]]):
                it["problem"] = "叠在合不了的 " + "、".join(f"#{m}" for m in below[it["pr"]] if m in bad) + " 后面"
                bad.add(it["pr"])
        return {"items": items, "merged": [n for n, x in info.items() if x.get("state") == "MERGED"]}

    def ship(self, prs):
        """上线（#156 第二轮、#207）：先检查，有合不了的就不开始；逐个合并，中途失败就停、不更新；全部合好最后更新一次。"""
        names = {x["pr"]: f"#{x['issue'] or x['pr']}" for x in (self.cache or {}).get("review") or []}
        prs = [int(n) for n in prs or []]
        if not prs:
            raise ValueError("先选要上线的需求。")
        return self._start("ship", lambda: self._ship(prs), prs=prs, names=[names.get(n, f"#{n}") for n in prs])

    def update(self):
        """已经合并了（比如 Agent 在对话里合的）、这台 Mac 还没用上：只做更新。"""
        return self._start("update", lambda: (self._update(), "")[1])

    def _ship(self, prs):
        self._step("检查能不能合并")
        c = self.check(prs)
        bad = [it for it in c["items"] if it["problem"]]
        if bad:                               # 网页上线前已经问过；这里是兜底，一个都不动
            raise RuntimeError("没有开始上线：" + "；".join(f"#{it['issue'] or it['pr']} {it['problem']}" for it in bad))
        done, heads, n = [], {}, len(c["items"])
        for i, it in enumerate(c["items"]):
            tag = f"#{it['issue'] or it['pr']}"
            try:
                self._merge(it, f"{tag}{f'（{i + 1}/{n}）' if n > 1 else ''}", heads)
            except Exception as e:
                rest = [f"#{x['issue'] or x['pr']}" for x in c["items"][i + 1:]]
                note = ("、".join(done) + " 已上线；" if done else "") + f"{tag} 没合上：{e}" + \
                    (f"；{'、'.join(rest)} 没动" if rest else "") + "。这台 Mac 没有更新"
                raise RuntimeError(note)
            done.append(tag)
            heads[it["pr"]] = it["head"]
        self._update()
        return "、".join(done) + " 已上线"

    def _sync(self, it, onto_from=None):
        """把分支对齐到最新的 main 并强推。onto_from：它叠在后面、刚合进去的那个分支原来的最后一次提交，只搬自己的提交（#207）。"""
        main = runtime._repo()
        wt = dict((b, x) for x, b in runtime.worktrees()[1:]).get(it["branch"])
        tmp = None
        if not wt:                            # 本机没有它的工作目录：临时开一个
            tmp = os.path.join(registry.HOME, "worktrees", f"ship-{it['pr']}")
            _run(["git", "-C", main, "worktree", "remove", "--force", tmp], timeout=60)
            r = _run(["git", "-C", main, "worktree", "add", "-q", "--detach", tmp, f"origin/{it['branch']}"], timeout=120)
            if r.returncode != 0:
                raise RuntimeError("没法取出这个分支：" + (r.stderr or "").strip()[-200:])
            wt = tmp
        try:
            _run(["git", "-C", wt, "fetch", "-q", "origin"], timeout=60)
            cmd = ["rebase", "-q", "--onto", "origin/main", onto_from] if onto_from else ["rebase", "-q", "origin/main"]
            r = _run(["git", "-C", wt, *cmd], timeout=120)
            if r.returncode != 0:
                _run(["git", "-C", wt, "rebase", "--abort"], timeout=30)
                raise RuntimeError("和别的改动有冲突，回到提这个需求的对话让 Agent 处理")
            r = _run(["git", "-C", wt, "push", "-q", "-f", "origin", f"HEAD:{it['branch']}"], timeout=120)
            if r.returncode != 0:
                raise RuntimeError("推送失败：" + (r.stderr or "").strip()[-200:])
        finally:
            if tmp:
                _run(["git", "-C", main, "worktree", "remove", "--force", tmp], timeout=60)

    def _merge(self, p, tag, heads=None):
        repo = _repo()
        wt = dict((b, x) for x, b in runtime.worktrees()[1:]).get(p["branch"])
        synced, deadline = 0, time.time() + 900
        base = next((heads[m] for m in p.get("after") or [] if m in (heads or {})), None)
        if base:                              # 叠在刚合进去的那个后面：只把自己的提交搬到最新的 main 上
            self._step(f"{tag} 对齐到最新代码")
            self._sync(p, onto_from=base)
            synced = 1
            time.sleep(15)
        self._step(f"{tag} 等检查通过")
        while True:                           # 前一个合进去后，后面的会落后：每次现问
            r = _gh(repo, ["pr", "view", str(p["pr"]), "--json", "state,statusCheckRollup,mergeStateStatus"])
            info = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
            if info.get("state") == "MERGED":
                break
            ms, st = info.get("mergeStateStatus"), _checks(info.get("statusCheckRollup"))
            if ms in ("BEHIND", "DIRTY") or (st == "fail" and not synced):
                if synced >= 2:
                    raise RuntimeError("和别的改动有冲突，回到提这个需求的对话让 Agent 处理" if ms == "DIRTY"
                                       else "落后于最新代码，没法自动同步")
                self._step(f"{tag} 同步最新代码")
                self._sync(p)
                synced += 1
                self._step(f"{tag} 等检查通过")
                time.sleep(15)
                continue
            if st == "fail":
                raise RuntimeError("检查没通过，回到提这个需求的对话让 Agent 看看")
            if st == "ok" and ms not in ("UNKNOWN", None):
                self._step(f"{tag} 合并")
                r = _gh(repo, ["pr", "merge", str(p["pr"]), "--squash", "--delete-branch"], timeout=120)
                if r.returncode == 0 or "already merged" in (r.stderr or ""):
                    break
                if "not up to date" not in (r.stderr or "") and "behind" not in (r.stderr or "").lower() \
                        and "not mergeable" not in (r.stderr or ""):
                    raise RuntimeError("合并失败：" + (r.stderr or r.stdout).strip()[-200:])
            if time.time() > deadline:
                raise RuntimeError("检查 15 分钟还没跑完")
            time.sleep(10)
        main = runtime._repo()
        _run(["git", "-C", main, "fetch", "-q", "origin"], timeout=60)
        _run(["git", "-C", main, "pull", "-q", "--ff-only"], timeout=120)
        trying = runtime.trying()
        if wt and not (trying and trying.get("branch") == p["branch"]):   # 正在试用它的：工作目录还在用，更新时切回 main 后再清
            _run(["git", "-C", main, "worktree", "remove", wt], timeout=60)
            _run(["git", "-C", main, "branch", "-D", p["branch"]], timeout=30)

    def _update(self):
        main = runtime._repo()
        self._step("更新这台 Mac")
        _run(["git", "-C", main, "pull", "-q", "--ff-only"], timeout=120)
        trying = runtime.trying()
        runtime.start_promote()               # 独立进程：装新版本、等各项目手头的活做完再重启
        if trying:                            # 切回 main 后，试用的工作目录（已合并的）可以清掉
            merged = trying.get("branch")
            r = _run(["git", "-C", main, "branch", "-r", "--list", f"origin/{merged}"], timeout=30)
            if not r.stdout.strip():          # 分支已经合并删掉了：等平台重启好（不再用这个目录）再清，平台重启也不影响
                subprocess.Popen(["sh", "-c", 'sleep 300; git -C "$1" worktree remove "$2" && git -C "$1" branch -D "$3"', "sh",
                                  main, trying["src"], merged], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
        runtime.set_try("main")               # 本机跑开发版的进程（网页平台也是）用上最新代码；平台会重启，网页断开一会儿

    # 平台重启后还能看到上一次上线的结果
    def _job_file(self):
        return os.path.join(runtime._stable(), "dev-job.json")

    def _save_job(self, job):
        try:
            os.makedirs(runtime._stable(), exist_ok=True)
            with open(self._job_file(), "w", encoding="utf-8") as f:
                json.dump(dict(job, state="done" if job.get("state") == "running" else job.get("state")), f, ensure_ascii=False)
        except OSError:
            pass

    def _load_job(self):
        try:
            with open(self._job_file(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
