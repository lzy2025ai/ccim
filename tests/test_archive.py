import os, tarfile, tempfile, unittest, zipfile

from ccim import docs


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_zip_and_tar(self):
        z = os.path.join(self.d, "包.zip")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("目录/说明.txt", "hi")
        t = os.path.join(self.d, "包.tar.gz")
        src = os.path.join(self.d, "说明.txt")
        open(src, "w").write("abc")
        with tarfile.open(t, "w:gz") as f:
            f.add(src, "目录/说明.txt")
        for p, size in ((z, 2), (t, 3)):
            v = docs.view(p)
            self.assertEqual(v["kind"], "archive")
            self.assertIn({"n": "目录/说明.txt", "s": size, "d": False}, v["entries"])

    def test_not_snapshotted_and_bad(self):
        p = os.path.join(self.d, "坏.zip")
        open(p, "w").write("not a zip")
        self.assertIsNone(docs.snapshot(self.d, "c", p))
        with self.assertRaises(docs.Problem):
            docs.view(p)


if __name__ == "__main__":
    unittest.main()
