"""任务面板「等你处理」「正在干活」的分栏（#390）：直接取 web.html 里的判断跑，覆盖在干活时等批准的对话。"""
import json
import os
import re
import shutil
import subprocess
import unittest

HTML = os.path.join(os.path.dirname(__file__), "..", "ccim", "web.html")


def classify(chats):
    src = open(HTML, encoding="utf8").read()
    defs = [re.search(p, src, re.S | re.M).group(0) for p in
            (r"^const working = .*?;$", r"^const showTag = .*?;", r"^const isOpen = .*?\)\)\)\);$")]
    work = re.search(r"const work = tops\.filter\((.*?)\)\.sort", src).group(1)
    js = "\n".join(defs) + f"""
const work = {work};
console.log(JSON.stringify({json.dumps(chats, ensure_ascii=False)}.map(c => ({{
  wait: !!isOpen(c), work: !!work(c), tag: !!showTag(c)}}))));"""
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(shutil.which("node"), "没有 node")
class BoardOpenTests(unittest.TestCase):
    def check(self, chat, wait, work, tag):
        self.assertEqual(classify([chat])[0], {"wait": wait, "work": work, "tag": tag}, chat)

    def test_ask_while_busy(self):
        self.check({"busy": True, "attention": "ask", "answered": True}, True, False, True)
        self.check({"busy": True, "attention": "ask", "answered": False}, True, False, True)

    def test_ask_while_crew_working(self):
        self.check({"busy": False, "crew": [{"agent": "a"}], "attention": "ask"}, True, False, True)

    def test_ask_idle(self):
        self.check({"attention": "ask"}, True, False, True)

    def test_ask_archived_leaves_wait(self):
        self.assertFalse(classify([{"busy": True, "attention": "ask", "archived": True}])[0]["wait"])

    def test_plain_working_only_in_work(self):
        self.check({"busy": True, "attention": None, "answered": True, "brief": "x"}, False, True, False)
        self.check({"crew": [{"agent": "a"}], "attention": None, "answered": True}, False, True, False)

    def test_other_attention_unchanged(self):
        self.check({"attention": "err", "answered": False}, True, False, True)
        self.check({"attention": "done", "answered": True}, True, False, True)
        self.check({"attention": "done", "answered": False}, False, False, True)
        self.check({"busy": True, "attention": "err"}, False, False, False)

    def test_seen_answer_stays(self):
        self.check({"attention": None, "answered": True}, True, False, False)

    def test_old_machine_without_answered(self):
        self.check({"attention": None, "brief": "做完了"}, True, False, False)
        self.check({"attention": None, "brief": "做完了", "schedule": "s"}, False, False, False)


if __name__ == "__main__":
    unittest.main()
