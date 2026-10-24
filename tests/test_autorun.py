import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao import autorun
from litzaobao.gui import TRANS_FIELDS
from litzaobao.translate import ENGINES


class FakeCompleted:
    def __init__(self, returncode=0, stderr=b"", stdout=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class TestAutorun(unittest.TestCase):
    def test_task_command_frozen_uses_exe(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", r"C:\Apps\lit-zaobao.exe"):
            cmd = autorun._task_command()
        self.assertIn("lit-zaobao.exe", cmd)
        self.assertIn("today", cmd)

    def test_task_command_source_uses_botpy(self):
        with patch.object(sys, "frozen", False, create=True):
            cmd = autorun._task_command()
        self.assertIn("bot.py", cmd)

    def test_valid_time(self):
        self.assertTrue(autorun._valid_time("08:30"))
        self.assertTrue(autorun._valid_time("8:30"))
        self.assertFalse(autorun._valid_time("25:00"))
        self.assertFalse(autorun._valid_time("8点"))

    def test_register_ok(self):
        with patch.object(autorun.subprocess, "run", return_value=FakeCompleted(0)) as m:
            ok, msg = autorun.register_task("9:05")
        self.assertTrue(ok)
        self.assertIn("09:05", msg)          # 补零
        args = m.call_args.args[0]
        self.assertIn("/ST", args)
        self.assertIn("09:05", args)

    def test_register_fail(self):
        with patch.object(autorun.subprocess, "run",
                          return_value=FakeCompleted(1, stderr="拒绝访问".encode("gbk"))):
            ok, msg = autorun.register_task("08:30")
        self.assertFalse(ok)
        self.assertIn("拒绝访问", msg)

    def test_task_exists(self):
        with patch.object(autorun.subprocess, "run", return_value=FakeCompleted(0)):
            self.assertTrue(autorun.task_exists())
        with patch.object(autorun.subprocess, "run", return_value=FakeCompleted(1)):
            self.assertFalse(autorun.task_exists())


class TestTransFieldSpec(unittest.TestCase):
    def test_every_engine_covered(self):
        for engine in ENGINES:
            self.assertIn(engine, TRANS_FIELDS)

    def test_fields_have_credentials_only_where_needed(self):
        self.assertEqual(TRANS_FIELDS["llm"], [])      # 复用 LLM key
        self.assertEqual(TRANS_FIELDS["google"], [])   # 免 key
        self.assertIn("deepl_api_key", [f for f, _, _ in TRANS_FIELDS["deepl"]])
        self.assertEqual(len(TRANS_FIELDS["baidu"]), 2)
        self.assertEqual(len(TRANS_FIELDS["youdao"]), 2)


if __name__ == "__main__":
    unittest.main()
