"""侧边对话多标签和两边互相知道对方在做什么（#476）。不跑真实对话。"""
import os
import unittest

from ccim import agents, session
from tests import test_side_mention as base


class SideTabsTests(base.SideMentionTests):                          # 只借它的 setUp

    def test_many_sides_close_reopen_delete(self):
        pf = self.pf
        main = self.group()
        a = pf.run(pf.side_chat(main))
        self.assertEqual(pf.run(pf.side_chat(main)), a)            # 不带 fresh：接着用开着的
        b = pf.run(pf.side_chat(main, fresh=True))
        self.assertNotEqual(a, b)
        self.assertEqual([r["id"] for r in pf.sides(main)["open"]], [a, b])   # 以前的不删
        self.assertNotIn("title", pf.chats()[b])                    # 标题等第一句提问
        pf.channel.logs.append(b, {"type": "user", "id": "u9", "text": "> 引用\n这里为什么报错"})
        pf.channel.logs.append(b, {"type": "answer", "text": "因为……"})
        pf.run(pf.side_close(b))
        s = pf.sides(main)
        self.assertEqual([r["id"] for r in s["open"]], [a])
        self.assertEqual([r["id"] for r in s["closed"]], [b])
        self.assertEqual(pf.run(pf.side_close(a)), {"dropped": True})   # 没说过话的直接删
        self.assertNotIn(a, pf.chats())
        pf.run(pf.side_close(b, closed=False))
        self.assertEqual([r["id"] for r in pf.sides(main)["open"]], [b])
        pf.run(pf.delete_chat(main))
        self.assertNotIn(b, pf.chats())                             # 跟着主对话删

    def test_old_side_title(self):
        pf = self.pf
        main = self.group()
        s = pf.run(pf.side_chat(main))
        chats = pf.chats()
        chats[s]["title"] = "侧边对话"                                # 以前开的
        pf.store.write(chats)
        pf.channel.logs.append(s, {"type": "user", "id": "u9", "text": "它在做什么"})
        self.assertEqual(pf.sides(main)["open"][0]["title"], "它在做什么")

    def test_main_sees_side_work(self):
        pf = self.pf
        main = self.group()
        s = pf.run(pf.side_chat(main))
        cwd = agents.cwd(agents.get(self.main_agent))
        pf.channel.logs.append(s, {"type": "user", "id": "u9", "text": "顺手改个错字"})
        pf.channel.logs.append(s, {"type": "answer", "text": "改好了", "steps": ["读取 a.md", "修改 a.md", "运行：git diff"],
                                   "changed": [{"path": os.path.join(cwd, "docs", "a.md")}]})
        news = pf._side_news(main)
        self.assertIn("docs/a.md", news)
        self.assertIn("git diff", news)
        self.assertEqual(pf._side_news(main), "")                   # 带过一次就不再带
        pf.channel.progress_of[s] = {"steps": ["修改 b.md"], "writing": [os.path.join(cwd, "b.md")]}
        self.assertIn("（正在做）", pf._side_news(main))

    def test_side_sees_main_files(self):
        pf = self.pf
        main = self.group()
        s = pf.run(pf.side_chat(main))
        cwd = agents.cwd(agents.get(self.main_agent))
        pf.channel.progress_of[main] = {"steps": ["修改 app.py"], "text": "", "writing": [os.path.join(cwd, "app.py")],
                                        "changed": [os.path.join(cwd, "lib", "x.py")]}
        ctx = pf._side_ctx(s, pf.meta(s))
        self.assertIn("app.py、lib/x.py", ctx)
        self.assertIn("不要提交", ctx)

    def test_side_can_write(self):
        pf = self.pf
        main = self.group()
        s = pf.run(pf.side_chat(main))
        e = self.real_bridge(s).entry
        self.assertTrue(e.get("side"))
        self.assertNotIn("只能看", e["role"])
        self.assertFalse(hasattr(session, "SIDE_OFF"))
        self.assertTrue(pf.chat_view(s)["perm_ok"])



for _n in [n for n in dir(base.SideMentionTests) if n.startswith("test_")]:
    if _n not in SideTabsTests.__dict__:
        setattr(SideTabsTests, _n, None)                             # 父类的测试不在这里重跑

if __name__ == "__main__":
    unittest.main()
