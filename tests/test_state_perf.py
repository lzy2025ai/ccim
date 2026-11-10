"""#440：找对话记录的缓存、同一次请求里路径只解析一遍。"""
import os, tempfile, unittest
from unittest import mock

from ccim import handoff, registry


class TranscriptPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, ".claude", "projects")
        os.makedirs(os.path.join(self.base, "a"))
        p = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        handoff._PATHS.clear()

    def write(self, d, sid):
        os.makedirs(os.path.join(self.base, d), exist_ok=True)
        p = os.path.join(self.base, d, sid + ".jsonl")
        open(p, "w").close()
        return p

    def test_found_once_then_no_scan(self):
        p = self.write("a", "s1")
        self.assertEqual(handoff.transcript_path("s1"), p)
        with mock.patch("os.listdir", side_effect=AssertionError):
            self.assertEqual(handoff.transcript_path("s1"), p)

    def test_moved_file_is_found_again(self):
        p = self.write("a", "s1")
        handoff.transcript_path("s1")
        os.remove(p)
        q = self.write("b", "s1")
        self.assertEqual(handoff.transcript_path("s1"), q)

    def test_miss_is_remembered_briefly(self):
        self.assertIsNone(handoff.transcript_path("s2"))
        with mock.patch("os.listdir", side_effect=AssertionError):
            self.assertIsNone(handoff.transcript_path("s2"))
        q = self.write("b", "s2")
        with mock.patch.object(handoff, "MISS_TTL", 0):
            self.assertEqual(handoff.transcript_path("s2"), q)

    def test_miss_does_not_hide_file_in_cwd(self):
        self.assertIsNone(handoff.transcript_path("s3", "/x/y"))
        q = self.write("-x-y", "s3")
        self.assertEqual(handoff.transcript_path("s3", "/x/y"), q)


class Key(unittest.TestCase):
    def test_realpath_once_in_snapshot(self):
        with mock.patch("os.path.realpath", wraps=os.path.realpath) as rp, registry.snapshot():
            a = registry.key("/tmp")
            self.assertEqual(registry.key("/tmp"), a)
        self.assertEqual(rp.call_count, 1)

    def test_outside_snapshot_not_kept(self):
        with mock.patch("os.path.realpath", wraps=os.path.realpath) as rp:
            registry.key("/tmp"); registry.key("/tmp")
        self.assertEqual(rp.call_count, 2)


class ClientGone(unittest.TestCase):
    def call(self, fn, send):
        from ccim import platform as P
        h = mock.MagicMock()
        h._guard.return_value = True
        h._send.side_effect = send
        P._handler(mock.MagicMock())._api(h, fn)
        return h

    def test_write_after_disconnect_is_not_a_server_error(self):
        h = self.call(lambda: {"a": 1}, BrokenPipeError())
        self.assertEqual(h._st, 499)
        self.assertEqual(h._send.call_count, 1)

    def test_timeout_inside_handler_is_still_a_server_error(self):
        def boom():
            raise TimeoutError("x")
        h = self.call(boom, None)
        self.assertEqual(h._send.call_args[0][0], 500)


if __name__ == "__main__":
    unittest.main()
