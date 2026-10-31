import os, tempfile, unittest
from unittest.mock import patch

from ccim import registry, slash


class SlashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude", "skills", "todo"))
        with open(os.path.join(self.home, ".claude", "skills", "todo", "SKILL.md"), "w") as f:
            f.write("---\nname: todo\ndescription: 记待办、看待办。用户说「记个待办」时使用。\n---\n正文")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.proj, ".claude", "commands"))
        with open(os.path.join(self.proj, ".claude", "commands", "ship.md"), "w") as f:
            f.write("# 上线这个项目\n")
        self.p = [patch.object(registry, "HOME", os.path.join(self.tmp, "ccim")), patch.dict(os.environ, {"HOME": self.home})]
        for x in self.p:
            x.start()
        os.makedirs(registry.HOME)

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_listing(self):
        slash.remember(self.proj, {"skills": ["todo", "code-review", "gone"], "plugins": []},
                       [{"name": "code-review", "description": "Review the current diff. More text."},
                        {"name": "todo", "description": "全局待办 (user)"}])
        got = {x["name"]: x for x in slash.listing(self.proj)}
        self.assertEqual(got["todo"], {"name": "todo", "desc": "记待办、看待办", "src": "本机"})
        self.assertEqual(got["ship"]["src"], "项目")
        self.assertEqual(got["ship"]["desc"], "上线这个项目")
        self.assertEqual(got["code-review"], {"name": "code-review", "desc": "Review the current diff", "src": "Claude Code"})
        self.assertEqual(got["gone"]["desc"], "")
        self.assertEqual([x["src"] for x in slash.listing(self.proj)][0], "项目")

    def test_no_session_yet(self):
        got = {x["name"] for x in slash.listing("")}
        self.assertIn("todo", got)
        self.assertNotIn("ship", got)


if __name__ == "__main__":
    unittest.main()
