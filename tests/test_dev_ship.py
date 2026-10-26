"""开发页批量上线（#207）：先检查、叠在一起的按顺序、中途合不上的跳过、其余照常上线（第二轮）。不连 GitHub，用本仓库 main 上的两个提交假装是两个 PR。"""
import json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccim import devpanel, runtime

main = runtime._repo()
git = lambda *a: subprocess.run(["git", "-C", main, *a], capture_output=True, text=True).stdout.strip()
A, B = git("rev-parse", "HEAD~2"), git("rev-parse", "HEAD")      # B 里包含 A：B 叠在 A 后面
draft = set()


def fake(repo, args, timeout=60):
    n = int(args[2])
    head = {900: A, 901: B}[n]
    return subprocess.CompletedProcess(args, 0, json.dumps({
        "number": n, "title": f"需求 #{n + 1}", "state": "OPEN", "isDraft": n in draft, "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [], "headRefName": "main", "headRefOid": head, "closingIssuesReferences": []}), "")


devpanel._gh = fake
devpanel._run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "") if "fetch" in cmd \
    else subprocess.run(cmd, capture_output=True, text=True)
dev = object.__new__(devpanel.Dev)
dev.job, dev._publish = {}, lambda: None

r = dev.check([901, 900])["items"]
assert [x["pr"] for x in r] == [900, 901] and r[1]["after"] == [900] and not any(x["problem"] for x in r), r

draft.add(900)
r = dev.check([901, 900])["items"]
assert r[0]["problem"] == "还是草稿" and "900" in r[1]["problem"], r
draft.clear()

calls = []


def merge(it, tag, heads):
    calls.append((it["pr"], dict(heads)))
    if it["pr"] == 901:
        raise RuntimeError("检查没通过")


dev._merge, dev._update = merge, lambda **k: calls.append("UPDATE")
assert dev._ship([901, 900]) == "#901 已上线", calls          # 901 合不上：跳过它，已合的 900 照常更新
assert calls == [(900, {}), (901, {900: A}), "UPDATE"], calls
assert [m["tag"] for m in dev.job["missed"]] == ["#902"] and "检查没通过" in dev.job["missed"][0]["why"], dev.job

calls.clear()
dev._merge = lambda it, tag, heads: (calls.append(it["pr"]), (_ for _ in ()).throw(RuntimeError("冲突")))
try:
    dev._ship([901, 900])                                     # 900 合不上，叠在它后面的 901 也跳过，一个都没合上就不更新
    raise AssertionError("应该失败")
except RuntimeError as e:
    assert "一个都没合上" in str(e), e
assert calls == [900] and "叠在没合上的 #901 后面" in dev.job["missed"][1]["why"], (calls, dev.job)

calls.clear()
dev._merge = lambda it, tag, heads: calls.append(it["pr"])
assert dev._ship([901, 900]) == "#901、#902 已上线" and calls == [900, 901, "UPDATE"], calls
print("ok")
