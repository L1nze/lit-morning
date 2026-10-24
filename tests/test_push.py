import json
import unittest
from unittest.mock import patch

from litzaobao.push import (configured_channels, push_configured, push_wechat,  # noqa: F401
                            serverchan, smtp, wecom, wxpusher)
from litzaobao.push import apply_push_save


class TestServerChan(unittest.TestCase):
    def test_url_and_payload(self):
        url, form = serverchan.build_payload("SCT123", "标题", "正文")
        self.assertEqual(url, "https://sctapi.ftqq.com/SCT123.send")
        self.assertEqual(form["title"], "标题")
        self.assertEqual(form["desp"], "正文")

    def test_truncation(self):
        _, form = serverchan.build_payload("k", "t", "x" * 40000)
        self.assertLessEqual(len(form["desp"]), serverchan.MAX_DESP + 50)
        self.assertIn("已截断", form["desp"])

    def test_truncation_with_full_url(self):
        _, form = serverchan.build_payload("k", "t", "x" * 40000, "http://full.example")
        self.assertIn("[📖 完整版](http://full.example)", form["desp"])
        self.assertLessEqual(len(form["desp"]), serverchan.MAX_DESP + 100)

    def test_missing_key(self):
        ok, msg = serverchan.push("", "t", "d")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_success_path(self):
        with patch("litzaobao.push.serverchan.http_post",
                   return_value=json.dumps({"code": 0, "message": ""})):
            ok, msg = serverchan.push("SCT1", "标题", "正文")
        self.assertTrue(ok)

    def test_error_path(self):
        with patch("litzaobao.push.serverchan.http_post",
                   return_value=json.dumps({"code": 400, "message": "bad key"})):
            ok, msg = serverchan.push("SCT1", "t", "d")
        self.assertFalse(ok)
        self.assertIn("bad key", msg)


class TestWecom(unittest.TestCase):
    def _long_md(self, n: int, line: str) -> str:
        return "\n".join(f"第{i}篇 {line}" for i in range(n))

    def test_split_byte_limit(self):
        md = self._long_md(300, "大模型智能体评估基准研究进展与实验数据综合分析。")
        msgs, truncated = wecom.build_segments("📚 文献早报", md)
        self.assertGreater(len(msgs), 1)
        self.assertFalse(truncated)
        for i, m in enumerate(msgs, 1):
            self.assertLessEqual(len(m.encode("utf-8")), wecom.MAX_BYTES,
                                 f"第 {i} 段超过 4096 字节")
            self.assertTrue(m.startswith(f"**📚 文献早报**（{i}/{len(msgs)}）\n\n"))
        self.assertIn("第299篇", msgs[-1])  # 分片不丢内容

    def test_split_with_full_url_keeps_limit(self):
        md = self._long_md(400, "大模型智能体基准评估数据填充内容，逐行累积。")
        url = "http://127.0.0.1:8000/digest-2026-10-24.html"
        msgs, _ = wecom.build_segments("标题", md, url)
        for m in msgs:
            self.assertLessEqual(len(m.encode("utf-8")), wecom.MAX_BYTES)
        self.assertIn(f"[📖 完整版]({url})", msgs[-1])
        self.assertNotIn("完整版", msgs[0])

    def test_single_part_no_link(self):
        msgs, truncated = wecom.build_segments("标题", "短正文", "http://x")
        self.assertEqual(len(msgs), 1)
        self.assertFalse(truncated)
        self.assertNotIn("完整版", msgs[0])

    def test_max_parts_truncation(self):
        md = "\n".join("长" * 120 for _ in range(1200))
        msgs, truncated = wecom.build_segments("标题", md)
        self.assertEqual(len(msgs), wecom._MAX_PARTS)
        self.assertTrue(truncated)
        for m in msgs:
            self.assertLessEqual(len(m.encode("utf-8")), wecom.MAX_BYTES)

    def test_push_multi_part_success(self):
        md = self._long_md(300, "智能体评估实验数据，多段验证内容。")
        calls = []

        def fake_post(url, payload, **kw):
            self.assertIn("key=abc-DEF123", url)
            self.assertEqual(payload["msgtype"], "markdown")
            content = payload["markdown"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), wecom.MAX_BYTES)
            calls.append(content)
            return json.dumps({"errcode": 0, "errmsg": "ok"})

        with patch("litzaobao.push.wecom.http_post", side_effect=fake_post):
            ok, msg = wecom.push("abc-DEF123", "标题", md)
        self.assertTrue(ok)
        self.assertGreater(len(calls), 1)

    def test_push_errcode(self):
        with patch("litzaobao.push.wecom.http_post",
                   return_value=json.dumps({"errcode": 93000, "errmsg": "invalid webhook"})):
            ok, msg = wecom.push("k-1", "t", "d")
        self.assertFalse(ok)
        self.assertIn("invalid webhook", msg)

    def test_missing_and_bad_key(self):
        ok, msg = wecom.push("", "t", "d")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)
        ok, msg = wecom.push("bad key!", "t", "d")
        self.assertFalse(ok)
        self.assertIn("非法字符", msg)


class TestWxPusher(unittest.TestCase):
    def test_parse_uids(self):
        self.assertEqual(wxpusher.parse_uids("UID_a, UID_b，UID_c"),
                         ["UID_a", "UID_b", "UID_c"])
        self.assertEqual(wxpusher.parse_uids(""), [])

    def test_payload(self):
        body = wxpusher.build_payload("AT_tok1", ["UID_x"], "标" * 100, "正文")
        self.assertEqual(body["appToken"], "AT_tok1")
        self.assertEqual(body["contentType"], 3)
        self.assertEqual(len(body["summary"]), 90)
        self.assertEqual(body["uids"], ["UID_x"])

    def test_payload_truncation_with_url(self):
        body = wxpusher.build_payload("AT", ["u"], "t", "x" * 40000, "http://full")
        self.assertLessEqual(len(body["content"]), wxpusher.MAX_CONTENT + 100)
        self.assertIn("[📖 完整版](http://full)", body["content"])

    def test_push_success(self):
        def fake_post(url, payload, **kw):
            self.assertEqual(url, wxpusher.API_URL)
            self.assertEqual(payload["uids"], ["UID_1", "UID_2"])
            return json.dumps({"code": 200, "msg": "ok"})

        with patch("litzaobao.push.wxpusher.http_post", side_effect=fake_post):
            ok, msg = wxpusher.push("AT_t", "UID_1,UID_2", "标题", "正文")
        self.assertTrue(ok)

    def test_push_error_and_missing(self):
        with patch("litzaobao.push.wxpusher.http_post",
                   return_value=json.dumps({"code": 400, "msg": "bad token"})):
            ok, msg = wxpusher.push("AT_t", "UID_1", "t", "d")
        self.assertFalse(ok)
        self.assertIn("bad token", msg)
        ok, msg = wxpusher.push("", "UID_1", "t", "d")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)
        ok, msg = wxpusher.push("AT_t", "", "t", "d")
        self.assertFalse(ok)
        self.assertIn("uid", msg.lower())


class TestSmtp(unittest.TestCase):
    def _cfg(self, **over):
        cfg = {"smtp_host": "smtp.example.com", "smtp_port": "465",
               "smtp_user": "bot@example.com", "smtp_pass": "secret",
               "smtp_to": "a@x.com, b@x.com"}
        cfg.update(over)
        return cfg

    def test_ssl_path(self):
        with patch("litzaobao.push.smtp.smtplib.SMTP_SSL") as ssl_cls:
            ok, msg = smtp.push(self._cfg(), "标题早报", "正文 markdown")
        self.assertTrue(ok)
        ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=30)
        s = ssl_cls.return_value.__enter__.return_value
        s.login.assert_called_once_with("bot@example.com", "secret")
        s.send_message.assert_called_once()

    def test_starttls_path(self):
        with patch("litzaobao.push.smtp.smtplib.SMTP_SSL") as ssl_cls, \
             patch("litzaobao.push.smtp.smtplib.SMTP") as cls:
            ok, msg = smtp.push(self._cfg(smtp_port="587"), "t", "m")
        self.assertTrue(ok)
        ssl_cls.assert_not_called()
        cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
        s = cls.return_value.__enter__.return_value
        s.starttls.assert_called_once_with()
        s.login.assert_called_once_with("bot@example.com", "secret")

    def test_default_port_is_ssl(self):
        with patch("litzaobao.push.smtp.smtplib.SMTP_SSL") as ssl_cls:
            ok, _ = smtp.push(self._cfg(smtp_port=""), "t", "m")
        self.assertTrue(ok)
        ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=30)

    def test_send_failure(self):
        with patch("litzaobao.push.smtp.smtplib.SMTP_SSL") as ssl_cls:
            ssl_cls.return_value.__enter__.return_value.login.side_effect = RuntimeError("auth fail")
            ok, msg = smtp.push(self._cfg(), "t", "m")
        self.assertFalse(ok)
        self.assertIn("auth fail", msg)

    def test_missing_and_bad_credentials(self):
        ok, msg = smtp.push({"smtp_host": "smtp.example.com"}, "t", "m")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)
        ok, msg = smtp.push(self._cfg(smtp_host="http://evil/x"), "t", "m")
        self.assertFalse(ok)
        self.assertIn("非法字符", msg)
        ok, msg = smtp.push(self._cfg(smtp_port="99999"), "t", "m")
        self.assertFalse(ok)
        self.assertIn("smtp_port", msg)

    def test_validate_offline(self):
        ok, msg = smtp.validate(self._cfg())
        self.assertTrue(ok)
        self.assertIn("2 个收件人", msg)
        ok, msg = smtp.validate(self._cfg(smtp_pass=""))
        self.assertFalse(ok)


class TestDispatch(unittest.TestCase):
    def test_primary_success_skips_fallback(self):
        cfg = {"provider": "serverchan", "serverchan_sendkey": "K",
               "fallback_provider": "pushplus", "pushplus_token": "T"}
        with patch("litzaobao.push.serverchan.push", return_value=(True, "Server酱推送成功")) as sc, \
             patch("litzaobao.push.pushplus.push", return_value=(True, "ok")) as pp:
            ok, msg = push_wechat(cfg, "t", "m")
        self.assertTrue(ok)
        sc.assert_called_once()
        pp.assert_not_called()

    def test_fallback_used_when_primary_fails(self):
        cfg = {"provider": "serverchan", "serverchan_sendkey": "K",
               "fallback_provider": "pushplus", "pushplus_token": "T"}
        with patch("litzaobao.push.serverchan.push", return_value=(False, "请求失败: x")) as sc, \
             patch("litzaobao.push.pushplus.push", return_value=(True, "PushPlus 推送成功")) as pp:
            ok, msg = push_wechat(cfg, "t", "m", "http://full")
        self.assertTrue(ok)
        self.assertIn("PushPlus", msg)
        sc.assert_called_once()
        # full_url 透传给备通道（供截断时附完整版链接）
        pp.assert_called_once_with("T", "t", "m", full_url="http://full")

    def test_both_fail_returns_summary(self):
        cfg = {"provider": "serverchan", "serverchan_sendkey": "K",
               "fallback_provider": "wecom", "wecom_key": "W"}
        with patch("litzaobao.push.serverchan.push", return_value=(False, "超时")), \
             patch("litzaobao.push.wecom.push", return_value=(False, "errcode!=0")):
            ok, msg = push_wechat(cfg, "t", "m")
        self.assertFalse(ok)
        self.assertIn("serverchan: 超时", msg)
        self.assertIn("wecom: errcode!=0", msg)

    def test_dedup_same_provider_and_fallback(self):
        cfg = {"provider": "pushplus", "pushplus_token": "T", "fallback_provider": "pushplus"}
        with patch("litzaobao.push.pushplus.push", return_value=(True, "ok")) as pp:
            ok, _ = push_wechat(cfg, "t", "m")
        self.assertTrue(ok)
        pp.assert_called_once()

    def test_unknown_provider(self):
        ok, msg = push_wechat({"provider": "feishu"}, "t", "m")
        self.assertFalse(ok)
        self.assertIn("未知渠道", msg)

    def test_none_still_returns_success(self):
        ok, msg = push_wechat({"provider": "none"}, "t", "m")
        self.assertTrue(ok)
        self.assertIn("关闭", msg)

    def test_none_primary_with_fallback(self):
        cfg = {"provider": "none", "fallback_provider": "wecom", "wecom_key": "W"}
        with patch("litzaobao.push.wecom.push", return_value=(True, "企业微信推送成功")) as wc:
            ok, msg = push_wechat(cfg, "t", "m")
        self.assertTrue(ok)
        wc.assert_called_once()
        self.assertIn("已跳过", msg)

    def test_push_configured_new_channels(self):
        self.assertTrue(push_configured({"provider": "wecom", "wecom_key": "k"}))
        self.assertTrue(push_configured(
            {"provider": "wxpusher", "wxpusher_app_token": "AT", "wxpusher_uid": "U"}))
        self.assertFalse(push_configured({"provider": "wxpusher", "wxpusher_app_token": "AT"}))
        self.assertTrue(push_configured({"provider": "none", "fallback_provider": "pushplus",
                                         "pushplus_token": "T"}))
        self.assertTrue(push_configured({
            "provider": "smtp", "smtp_host": "h", "smtp_user": "u",
            "smtp_pass": "p", "smtp_to": "a@b.c"}))
        self.assertFalse(push_configured({"provider": "serverchan"}))

    def test_configured_channels_order_and_dedup(self):
        cfg = {"provider": "pushplus", "pushplus_token": "T",
               "fallback_provider": "serverchan", "serverchan_sendkey": "K"}
        self.assertEqual(configured_channels(cfg), ["pushplus", "serverchan"])
        cfg["fallback_provider"] = "pushplus"
        self.assertEqual(configured_channels(cfg), ["pushplus"])
        cfg["fallback_provider"] = "wecom"  # 凭据未填：不出现
        self.assertEqual(configured_channels(cfg), ["pushplus"])

    def test_apply_push_save_new_fields(self):
        cfgd = {"push": {}}
        apply_push_save(cfgd, "wecom", "  KEY-1 ")
        self.assertEqual(cfgd["push"]["wecom_key"], "KEY-1")
        apply_push_save(cfgd, "wxpusher", "AT_x")
        self.assertEqual(cfgd["push"]["wxpusher_app_token"], "AT_x")
        apply_push_save(cfgd, "smtp", "whatever")
        self.assertEqual(cfgd["push"]["provider"], "smtp")  # smtp 无单一 key 字段
        self.assertNotIn("smtp_key", cfgd["push"])


if __name__ == "__main__":
    unittest.main()
