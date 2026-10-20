"""头像保存、兼容旧数据及内置资源白名单；不读取真实登记表。"""
import os
import tempfile
import unittest
from unittest.mock import patch

from ccim import agents, registry
from ccim.platform import static_file


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = patch.object(registry, 'HOME', self.tmp.name)
        self.reg = patch.object(registry, 'REG', os.path.join(self.tmp.name, 'registry.json'))
        self.home.start()
        self.reg.start()
        self.addCleanup(self.home.stop)
        self.addCleanup(self.reg.stop)

    def test_save_reload_change_and_clear(self):
        a = agents.create(name='测试', avatar=agents.AVATARS[0])
        self.assertEqual(agents.get(a['id'])['avatar'], agents.AVATARS[0])
        agents.update(a['id'], role='负责调研')
        self.assertEqual(agents.get(a['id'])['avatar'], agents.AVATARS[0])
        agents.update(a['id'], avatar=agents.AVATARS[-1])
        self.assertEqual(agents.get(a['id'])['avatar'], agents.AVATARS[-1])
        agents.update(a['id'], avatar=None)
        self.assertIsNone(agents.get(a['id'])['avatar'])
        self.assertEqual(agents.get(a['id'])['seal'], '测试')

    def test_legacy_agent_unchanged(self):
        a = agents.create(name='旧助手', color='lu')
        self.assertNotIn('avatar', a)
        agents.update(a['id'], role='继续工作')
        self.assertEqual(agents.get(a['id'])['color'], 'lu')
        self.assertNotIn('avatar', agents.get(a['id']))

    def test_untrusted_values_cannot_become_image_urls(self):
        for value in ['../../registry.json', 'https://example.com/a.webp', '<img>', [], {}]:
            with self.subTest(value=value):
                self.assertIsNone(agents.create(name='测试', avatar=value)['avatar'])
        self.assertIsNone(static_file('/avatars/../registry.json'))
        self.assertIsNone(static_file('/avatars/unknown.webp'))

    def test_every_builtin_is_packaged_webp(self):
        self.assertEqual(len(set(agents.AVATARS)), 16)
        for avatar in agents.AVATARS:
            data, mime = static_file('/avatars/' + avatar + '.webp')
            self.assertEqual(mime, 'image/webp')
            self.assertEqual(data[:4], b'RIFF')
            self.assertEqual(data[8:12], b'WEBP')
            self.assertLess(len(data), 20000)


if __name__ == '__main__':
    unittest.main()
