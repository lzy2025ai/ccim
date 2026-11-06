"""DeepSeek Harness 底座（#424）：用假的 dsh 程序跑一轮，不联网。"""
import asyncio, json, os, stat, tempfile, unittest

from ccim import dsh, history, registry
from ccim.dsh import DshChat

FAKE = """#!/bin/sh
cat > /dev/null
echo '{"type":"session","sessionId":"session-abc-1"}'
echo '{"type":"tool_call","tool":"bash","input":{"command":"ls"}}'
echo 'not json'
echo '{"type":"text","text":"你好"}'
echo '{"type":"final","text":"你好"}'
"""


class Basics(unittest.TestCase):
    def test_sid_key(self):
        self.assertEqual(registry.sid_key("dsh"), DshChat.SID_KEY)
        self.assertEqual(registry.sid_key("codex"), "codex_session_id")
        self.assertEqual(registry.sid_key("claude"), "session_id")

    def test_describe(self):
        self.assertEqual(dsh.describe("bash", {"command": "ls"}), "运行：ls")
        self.assertEqual(dsh.describe("web_search", {}), "使用 web_search")

    def test_key_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            root, key = dsh.ROOT, dsh.KEY
            dsh.ROOT, dsh.KEY = d, os.path.join(d, "api-key")
            self.addCleanup(lambda: (setattr(dsh, "ROOT", root), setattr(dsh, "KEY", key)))
            self.assertIsNone(dsh.read_key())
            dsh.save_key(" sk-test\n")
            self.assertEqual(dsh.read_key(), "sk-test")
            self.assertEqual(stat.S_IMODE(os.stat(dsh.KEY).st_mode), 0o600)

    def test_history_ignores_odd_ids(self):
        self.assertIsNone(history.dsh_file("../../etc/passwd"))
        self.assertEqual(history.dsh("../../etc/passwd"), [])


class Turn(unittest.TestCase):
    def test_one_turn(self):
        with tempfile.TemporaryDirectory() as d:
            old = registry.HOME, dsh.ROOT, dsh.KEY, dsh.BIN, dsh.find_dsh, dsh.find_node
            self.addCleanup(lambda: (setattr(registry, "HOME", old[0]), setattr(dsh, "ROOT", old[1]), setattr(dsh, "KEY", old[2]),
                                     setattr(dsh, "BIN", old[3]), setattr(dsh, "find_dsh", old[4]), setattr(dsh, "find_node", old[5])))
            registry.HOME = d
            dsh.ROOT, dsh.KEY = os.path.join(d, "dsh"), os.path.join(d, "dsh", "api-key")
            dsh.save_key("sk-test")
            exe = os.path.join(d, "fake.sh")
            with open(exe, "w") as f:
                f.write(FAKE)
            os.chmod(exe, 0o755)
            dsh.find_dsh, dsh.find_node = (lambda: exe), (lambda: "/bin/sh")
            finished, saved = [], {}

            class B:
                path, entry = d, {}
                channel = type("C", (), {"name": "fake", "label": "测试", "style": "简单"})()
                def saved(self, chat_id): return {}
                def save(self, chat_id, **kw): saved.update(kw)

            chat = DshChat.__new__(DshChat)
            chat.b, chat.chat_id, chat.chat_type = B(), "c", "p2p"
            chat.session_id, chat.fork_from, chat.stopping, chat.proc, chat.turn = None, None, False, None, None
            chat.as_fence = None
            chat.ccim_env = lambda: {"PATH": os.environ["PATH"]}

            async def tick(t, r): await asyncio.sleep(3600)
            async def finish(t, result, error, r): finished.append((result, error, list(t.steps)))
            async def files(text): return text
            chat._tick, chat._finish, chat._send_files = tick, finish, files
            asyncio.run(chat._turn_inner("你好", None))
            self.assertEqual(finished, [("你好", None, ["运行：ls"])])
            self.assertEqual(saved.get("dsh_session_id"), "session-abc-1")
            rows = history.dsh("session-abc-1")
            self.assertEqual([r["type"] for r in rows], ["user", "answer"])


if __name__ == "__main__":
    unittest.main()
