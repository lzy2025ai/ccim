import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ccim import perf, registry, share


class PerfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = os.path.realpath(self.tmp.name)
        ps = [patch.object(registry, "HOME", os.path.join(home, ".ccim")),
              patch.object(registry, "REG", os.path.join(home, ".ccim", "registry.json")),
              patch.dict(os.environ, {"HOME": home})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        return perf._read(0)

    def serve(self):
        from ccim import platform
        from ccim.peers import Peers
        h = os.path.join(registry.HOME, "platform")
        os.makedirs(h, exist_ok=True)
        entry = {"kind": "platform", "channel": "web", "port": 18978}
        pf = platform.Platform(h, entry)
        pf.channel = platform.WebChannel(h, entry, None, {"platform": pf})
        pf.channel.platform = pf
        pf.channel.events.tag = lambda ev: share.tag(pf, ev)
        pf.loop = asyncio.new_event_loop()
        threading.Thread(target=pf.loop.run_forever, daemon=True).start()
        self.addCleanup(pf.loop.call_soon_threadsafe, pf.loop.stop)
        pf.peers = Peers(pf)
        pf.external_chats = lambda *a, **k: []
        server = pf.serve()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def req(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        h = {"X-CCIM": "1", "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}"), r.getheader("Server-Timing")
        c.close()
        return out

    def test_track_records_and_catches_slow_stack(self):
        with perf.Track("api", "GET /fast"):
            pass
        with perf.Track("api", "GET /slow", q={"id": "x"}) as t:
            time.sleep(1.3)
            t.status = 200
        fast, slow = self.rows()
        self.assertEqual(fast["n"], "GET /fast")
        self.assertNotIn("stacks", fast)
        self.assertEqual((slow["st"], slow["q"]), (200, {"id": "x"}))
        self.assertGreaterEqual(slow["ms"], 1300)
        self.assertTrue(slow["id"] and slow["stacks"])
        self.assertTrue(any("test_perf" in line for line in slow["stacks"][0]["stack"]))

        s = perf.summary(1)
        names = {g["n"]: g for g in s["groups"]}
        self.assertEqual(names["GET /slow"]["slow"], 1)
        self.assertEqual(s["slowest"][0]["n"], "GET /slow")
        self.assertTrue(s["hotspots"])

    def test_web_items_are_checked(self):
        perf.web([{"k": "state", "n": "取整体状态", "ms": 120, "server": 80},
                  {"k": "chat", "n": "打开对话", "ms": 2500, "ctx": {"chat": "w-1", "msgs": 40}},
                  {"k": "nope", "n": "x", "ms": 1}, {"k": "api", "n": "x", "ms": "bad"}, "junk"], machine="m1")
        a, b = self.rows()
        self.assertEqual((a["k"], a["server"]), ("web.state", 80))
        self.assertNotIn("chat", a)
        self.assertEqual((b["k"], b["chat"], b["from"]), ("web.chat", "w-1", "m1"))

    def test_rotation(self):
        with patch.object(perf, "MAX_BYTES", 200):
            for i in range(40):
                with perf.Track("api", f"GET /x{i}"):
                    pass
        self.assertTrue(os.path.exists(perf._file(1)) and os.path.exists(perf._file(2)))
        self.assertFalse(os.path.exists(perf._file(3)))
        self.assertLess(os.path.getsize(perf._file()), 400)

    def test_http_timing(self):
        port = self.serve()
        st, r, timing = self.req(port, "GET", "/api/board/day?d=2026-10-20")
        self.assertEqual(st, 200, r)
        self.assertRegex(timing or "", r"^app;dur=[\d.]+$")
        st, r, _ = self.req(port, "POST", "/api/perf", {"items": [{"k": "load", "n": "打开网页", "ms": 900}]})
        self.assertEqual(st, 200, r)
        time.sleep(0.2)
        st, r, timing = self.req(port, "GET", "/api/perf?hours=1")
        self.assertIsNone(timing)                    # 看性能数据本身不记
        names = {g["n"] for g in r["groups"]}
        self.assertIn("GET /api/board/day", names)
        self.assertIn("打开网页", names)
        self.assertNotIn("GET /api/perf", names)
        self.assertNotIn("POST /api/perf", names)

        from ccim import board
        orig = board.day

        def slow_day(*a, **k):
            time.sleep(1.2)
            return orig(*a, **k)
        with patch.object(board, "day", slow_day):
            self.req(port, "GET", "/api/board/day?d=2026-10-21")
        for _ in range(50):                          # 响应发出去之后才写记录
            got = [x for x in self.rows() if x.get("q", {}).get("d") == "2026-10-21"]
            if got:
                break
            time.sleep(0.05)
        r = got[0]
        self.assertTrue(any("platform.py" in line for line in r["stacks"][0]["stack"]), r)


if __name__ == "__main__":
    unittest.main()
