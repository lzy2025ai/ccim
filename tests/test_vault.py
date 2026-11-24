import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ccim import devices, registry, space, vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(registry, "HOME", str(self.home / "platform")).start()
        self.root = self.home / "笔记"
        self.root.mkdir()
        self.id = vault.device(None, None, {"op": "register", "path": str(self.root)})["id"]

    def call(self, op, **args):
        return vault.device(None, None, dict(args, op=op, vault=self.id))

    def test_markdown_from_disk_and_stale_save(self):
        self.call("create", name="项目", folder=True)
        note = self.call("create", path="项目", name="观察")["path"]
        file = self.root / note
        file.write_bytes(b"\xef\xbb\xbf# Old\r\n")
        first = self.call("read", path=note)
        result = self.call("save", path=note, body="# New\n", version=first["version"])
        self.assertEqual(file.read_bytes(), b"\xef\xbb\xbf# New\r\n")
        self.assertEqual(self.call("save", path=note, body="# New\n", version=first["version"]), result)
        file.write_text("从外部写入的正文")
        self.assertTrue(self.call("save", path=note, body="旧编辑器的修改", version=result["version"])["conflict"])
        self.assertEqual(self.call("search", query="外部写入")["entries"][0]["path"], note)
        self.assertEqual(self.call("search", query="观察")["entries"][0]["path"], note)
        self.assertFalse(self.call("suggest", query="外部写入")["entries"])

    def test_scope_symlinks_and_share_changes(self):
        other = self.home / "outside"
        other.mkdir()
        (other / "outside.md").write_text("no")
        (self.root / "redirect").symlink_to(other)
        (self.root / "linked.md").symlink_to(other / "outside.md")
        self.assertEqual(self.call("list")["entries"], [])
        for path in ("redirect/outside.md", "../outside/outside.md", "/outside/outside.md", "linked.md"):
            with self.subTest(path=path), self.assertRaises(vault.Problem):
                self.call("read", path=path)
        with self.assertRaises(vault.Problem):
            vault.device("guest", self.root, {"op": "register", "path": str(other)})
        guest = vault.device("guest", self.root, {"op": "register", "path": str(self.root)})["id"]
        with self.assertRaises(vault.Problem):
            vault.device(None, None, {"op": "list", "vault": guest})
        with self.assertRaises(vault.Problem):
            vault.device("guest", other, {"op": "list", "vault": guest})

    def test_obsidian_daily_and_image_relative_to_note(self):
        obsidian = self.root / ".obsidian"
        obsidian.mkdir()
        (obsidian / "daily-notes.json").write_text(json.dumps({
            "folder": "日记", "format": "YYYY/MM/YYYY-MM-DD", "template": "模板"}))
        (obsidian / "app.json").write_text(json.dumps({"attachmentFolderPath": "./配图"}))
        (self.root / "模板.md").write_text("# {{title}}\n{{date:YYYY年MM月DD日}}")
        self.assertEqual(self.call("daily")["format"], "YYYY/MM/YYYY-MM-DD")
        name = "2026/11/2026-11-25"
        token = "{{date:YYYY年MM月DD日}}"
        self.assertEqual(self.call("daily_open", name=name), {"tokens": [token]})
        note = self.call("daily_open", name=name, values={token: "2026年11月25日"})
        self.assertEqual(note["path"], f"日记/{name}.md")
        self.assertEqual(note["body"], "# 2026-11-25\n2026年11月25日")
        self.assertEqual(self.call("daily_open", name=name), note)
        data = base64.b64encode(b"test image bytes").decode()
        saved = self.call("attach", path=note["path"], name="图片.png", data=data)
        self.assertEqual(saved["relative"], "配图/图片.png")
        self.assertTrue((self.root / saved["path"]).exists())
        self.assertEqual(self.call("asset", path=note["path"], ref="%E9%85%8D%E5%9B%BE/%E5%9B%BE%E7%89%87.png")["data"], data)

    def test_link_can_create_and_folder_actions_use_trash(self):
        self.call("create", name="子目录", folder=True)
        self.call("create", path="子目录", name="笔记")
        note = self.call("link", path="子目录/笔记.md", ref="新增/目标|显示名")["path"]
        self.assertEqual(note, "新增/目标.md")
        self.assertEqual(self.call("link", path="子目录/笔记.md", ref="目标#小标题")["path"], note)
        self.call("rename", path=note, name="改名")
        self.call("rename", path="新增", name="归档")
        self.assertTrue((self.root / "归档/改名.md").is_file())
        trash = self.home / "trash"
        with patch.object(devices, "trash_dir", return_value=str(trash)):
            self.call("trash", path="归档")
        self.assertFalse((self.root / "归档").exists())
        self.assertTrue((trash / "归档/改名.md").exists())

    def test_first_selection_retries_offline_and_preserves_existing_files(self):
        homes = {"first": self.home / "first", "second": self.home / "second"}
        machines = [{"id": key, "name": key, "online": key == "first"} for key in homes]
        for key, home in homes.items():
            with patch.object(registry, "HOME", str(home)):
                space.note_save(None, None, "同名", f"{key} 的原有正文")
                space.todo_add(None, f"{key} 的待办")
        (self.root / "同名.md").write_text("现有文件")

        def forward(mid, method, route, body):
            with patch.object(registry, "HOME", str(homes[mid])):
                d = space.view(None) if method == "GET" else vault.device(None, None, json.loads(body))
            return 200, "application/json", json.dumps(d).encode()

        first = vault.select(None, {"machine": "first", "path": str(self.root)}, machines, forward)
        self.assertEqual(first["imported"], 1)
        self.assertEqual([p["machine"] for p in first["pending"]], ["second"])
        machines[1]["online"] = True
        resumed = vault.select(None, {"retry": True}, machines, forward)
        self.assertEqual(resumed["imported"], 1)
        self.assertEqual(resumed["pending"], [])
        self.assertEqual(vault.select(None, {"retry": True}, machines, forward)["imported"], 0)
        self.assertEqual(sorted(p.read_text() for p in self.root.glob("*.md")),
                         sorted(["现有文件", "first 的原有正文", "second 的原有正文"]))
        with patch.object(registry, "HOME", str(homes["first"])):
            self.assertEqual(space.view(None, notes=False)["todos"][0]["text"], "first 的待办")
