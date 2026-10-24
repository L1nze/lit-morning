import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao.server import find_free_port


class TestFrozenPaths(unittest.TestCase):
    def test_frozen_root_points_to_exe_dir(self):
        """模拟 PyInstaller 环境：sys.frozen=True 时根目录必须是 exe 所在目录。"""
        import litzaobao.config as cfg_mod
        fake_exe = Path("C:/Apps/lit-zaobao/lit-zaobao.exe")
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(fake_exe)):
            root = cfg_mod._project_root()
        self.assertEqual(root, fake_exe.parent)

    def test_source_root_is_package_parent(self):
        import litzaobao.config as cfg_mod
        with patch.object(sys, "frozen", False, create=True):
            root = cfg_mod._project_root()
        self.assertTrue((root / "bot.py").exists())   # 源码布局下项目根含 bot.py


class TestFreePort(unittest.TestCase):
    def test_finds_bindable_port(self):
        import socket
        port = find_free_port(18000, 20)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))   # 能绑上才算数

    def test_skips_occupied(self):
        import socket
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 18050))
            port = find_free_port(18050, 20)
        self.assertNotEqual(port, 18050)


if __name__ == "__main__":
    unittest.main()
