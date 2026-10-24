import unittest
from datetime import datetime, timedelta

from litzaobao.daemon import _seconds_until
from litzaobao.push import push_configured


class TestSecondsUntil(unittest.TestCase):
    def test_future_time_today(self):
        now = datetime.now()
        target = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
        s = _seconds_until(f"{target.hour:02d}:{target.minute:02d}")
        self.assertGreater(s, 0)
        self.assertLessEqual(s, 2 * 3600 + 60)

    def test_past_time_rolls_to_tomorrow(self):
        now = datetime.now()
        past = (now - timedelta(minutes=10)).replace(second=0, microsecond=0)
        s = _seconds_until(f"{past.hour:02d}:{past.minute:02d}")
        self.assertGreater(s, 23 * 3600)  # 必然滚到明天

    def test_bad_format_exits(self):
        with self.assertRaises(SystemExit):
            _seconds_until("8点")


class TestPushConfigured(unittest.TestCase):
    def test_providers(self):
        self.assertFalse(push_configured({"provider": "serverchan"}))
        self.assertTrue(push_configured({"provider": "serverchan", "serverchan_sendkey": "SCT1"}))
        self.assertTrue(push_configured({"provider": "pushplus", "pushplus_token": "t"}))
        self.assertFalse(push_configured({"provider": "pushplus"}))
        self.assertFalse(push_configured({"provider": "none"}))


if __name__ == "__main__":
    unittest.main()
