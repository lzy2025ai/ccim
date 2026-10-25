import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import tokens


class TokensTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {"HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        tokens._FILES.clear()

    def write(self, rel, rows):
        path = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return path

    @staticmethod
    def msg(mid, ts, inp, out, read=0, made=0):
        return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read, "cache_creation_input_tokens": made}}}

    def test_same_message_counts_once(self):
        ts = "2026-10-20T04:00:00Z"
        day = tokens._day(ts)
        # 一条回答按内容块写成两行（前一行 output 不全），分叉又复制到另一个会话
        self.write(".claude/projects/p/a.jsonl", [self.msg("m1", ts, 10, 1, 500), self.msg("m1", ts, 10, 30, 500, 5)])
        self.write(".claude/projects/p/b.jsonl", [self.msg("m1", ts, 10, 30, 500, 5), self.msg("m2", "2026-10-20T05:00:00Z", 3, 7)])
        self.write(".claude/projects/p/a/subagents/agent-1.jsonl", [self.msg("m3", ts, 1, 1)])
        self.write(".codex/sessions/2026/10/20/rollout-2026-10-20T00-00-00-0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
            {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 120}, "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}}}])
        tokens.refresh()
        self.assertEqual(tokens.days()[day], [45 + 10 + 2 + 40, 500 + 80])
        self.assertEqual(tokens.of_sessions(["a"])[day], [45 + 2, 500])   # 子 agent 算到会话 a；复制过去的算最早的
        self.assertEqual(tokens.of_sessions(["b"])[day], [10, 0])
        self.assertEqual(tokens.of_sessions(["0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])[day], [40, 80])
        # 接着写的部分只读新增的
        self.write(".claude/projects/p/b.jsonl", [self.msg("m4", ts, 1, 1)])
        tokens.refresh()
        self.assertEqual(tokens.days()[day][0], 97 + 2)


if __name__ == "__main__":
    unittest.main()
