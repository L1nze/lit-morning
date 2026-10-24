import unittest


class TestGuiModule(unittest.TestCase):
    """GUI 单测只覆盖无窗口逻辑；真实窗口构造不进单测——Tcl 解释器对创建
    线程敏感，和服务器测试线程同进程会把整个测试跑崩（实测 Tcl_AsyncDelete）。
    窗口本体由 exe 实机验收。"""

    def test_import_no_side_effects(self):
        import litzaobao.gui as gui  # 导入不应创建窗口
        self.assertTrue(hasattr(gui, "App"))
        self.assertTrue(hasattr(gui, "SettingsDialog"))

    def test_single_instance_lock(self):
        from litzaobao.gui import _acquire_lock
        port = 19283   # 独立端口，与正在运行的 exe（锁 18731）互不干扰
        lock1 = _acquire_lock(port)
        self.assertIsNotNone(lock1)
        lock2 = _acquire_lock(port)   # 已被持有 → None（且 socket 已关闭，无 fd 泄漏）
        self.assertIsNone(lock2)
        lock1.close()
        lock3 = _acquire_lock(port)   # 释放后可再拿
        self.assertIsNotNone(lock3)
        lock3.close()


if __name__ == "__main__":
    unittest.main()
