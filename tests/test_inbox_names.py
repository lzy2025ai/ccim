"""收件箱文件名（#167）：同一秒里同名的不覆盖。"""
import os
import tempfile
import unittest

from ccim.channels.base import inbox_path


class InboxNameTests(unittest.TestCase):
    def test_same_name_same_second(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for _ in range(3):
                p = inbox_path(d, "image.png")
                open(p, "w").close()
                paths.append(os.path.basename(p))
            self.assertEqual(len(set(paths)), 3)
            self.assertTrue(paths[0].endswith("-image.png"))
            self.assertTrue(paths[1].endswith("-image-2.png") or paths[1] != paths[0])
            self.assertTrue(all(p.endswith(".png") for p in paths))


if __name__ == "__main__":
    unittest.main()
