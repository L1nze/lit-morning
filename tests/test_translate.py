import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao import translate as tr


class TestChunking(unittest.TestCase):
    def test_sentence_boundaries(self):
        text = "A" * 100 + ". " + "B" * 100 + ". " + "C" * 100 + "."
        chunks = tr.split_chunks(text, 150)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 150 for c in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))

    def test_empty_and_short(self):
        self.assertEqual(tr.split_chunks("", 100), [])
        self.assertEqual(tr.split_chunks("short.", 100), ["short."])


class TestEngines(unittest.TestCase):
    def test_google_parse(self):
        payload = [[["大模型智能体评估", "LLM agent evaluation", None, None, 10],
                    ["效果很好。", "works well.", None, None, 1]], None, "en"]
        with patch("litzaobao.translate.http_get", return_value=json.dumps(payload)):
            out = tr._google("LLM agent evaluation works well.", "zh-CN", {}, {})
        self.assertEqual(out, "大模型智能体评估效果很好。")

    def test_google_sorry_page_detected(self):
        with patch("litzaobao.translate.http_get", return_value="<html>Sorry...</html>"):
            with self.assertRaises(RuntimeError):
                tr._google("text", "zh-CN", {}, {})

    def test_mymemory_quota_detected(self):
        resp = {"responseData": {"translatedText": "MYMEMORY WARNING: YOU USED ALL"}}
        with patch("litzaobao.translate.http_get", return_value=json.dumps(resp)):
            with self.assertRaises(RuntimeError):
                tr._mymemory("text", "zh-CN", {}, {})

    def test_deepl_payload(self):
        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"translations": [{"text": "译文"}]})
        with patch("litzaobao.translate.http_post", side_effect=fake_post):
            out = tr._deepl("hello", "zh-CN", {"deepl_api_key": "dk"}, {})
        self.assertEqual(out, "译文")
        self.assertEqual(captured["auth_key"], "dk")
        self.assertEqual(captured["target_lang"], "ZH")

    def test_baidu_sign(self):
        cfg = {"baidu_appid": "app1", "baidu_key": "sekrit"}
        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"trans_result": [{"dst": "译文"}]})
        with patch("litzaobao.translate.http_post", side_effect=fake_post):
            tr._baidu("hello world", "zh-CN", cfg, {})
        expect = hashlib.md5(("app1" + "hello world" + captured["salt"] + "sekrit").encode()).hexdigest()
        self.assertEqual(captured["sign"], expect)

    def test_baidu_error_code(self):
        with patch("litzaobao.translate.http_post",
                   return_value=json.dumps({"error_code": "52003", "error_msg": "UNAUTHORIZED"})):
            with self.assertRaises(RuntimeError):
                tr._baidu("x", "zh-CN", {"baidu_appid": "a", "baidu_key": "b"}, {})

    def test_youdao_sign_short_input(self):
        cfg = {"youdao_appkey": "ak", "youdao_secret": "sk"}
        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"errorCode": "0", "translation": ["译文"]})
        with patch("litzaobao.translate.http_post", side_effect=fake_post):
            tr._youdao("short text", "zh-CN", cfg, {})
        # 短文本 input=原文：sha256(ak + "short text" + salt + curtime + sk)
        expect = hashlib.sha256(
            ("ak" + "short text" + captured["salt"] + captured["curtime"] + "sk").encode()
        ).hexdigest()
        self.assertEqual(captured["sign"], expect)

    def test_llm_engine(self):
        resp = {"choices": [{"message": {"content": "  大模型智能体的评估。 "}}]}
        llm_raw = {"active": "zhipu", "providers": {"zhipu": {"api_key": "k", "model": "glm-4-flash"}}}
        with patch("litzaobao.llm_providers.http_post", return_value=json.dumps(resp)):
            out = tr._llm("Evaluation of LLM agents.", "zh-CN", {}, llm_raw)
        self.assertEqual(out, "大模型智能体的评估。")

    def test_volc_parse(self):
        with patch("litzaobao.translate.http_post",
                   return_value=json.dumps({"translation": "火山译文"})):
            out = tr._volc("hello", "zh-CN", {}, {})
        self.assertEqual(out, "火山译文")
        with patch("litzaobao.translate.http_post", return_value=json.dumps({})):
            with self.assertRaises(RuntimeError):
                tr._volc("hello", "zh-CN", {}, {})

    def test_tencent_parse(self):
        resp = {"auto_translation": ["第一段。", "第二段。"]}
        with patch("litzaobao.translate.http_post", return_value=json.dumps(resp)):
            out = tr._tencent("hello", "zh-CN", {}, {})
        self.assertEqual(out, "第一段。\n第二段。")
        with patch("litzaobao.translate.http_post", return_value=json.dumps({"auto_translation": []})):
            with self.assertRaises(RuntimeError):
                tr._tencent("hello", "zh-CN", {}, {})

    def test_free_engines_in_fallback_order(self):
        self.assertEqual(tr._FALLBACK_ORDER[:3], ["llm", "volc", "tencent"])
        self.assertTrue(tr.engine_configured("volc", {}, {}))
        self.assertTrue(tr.engine_configured("tencent", {}, {}))


class TestChain(unittest.TestCase):
    def test_fallback_to_next_engine(self):
        llm_raw = {"active": "zhipu", "providers": {"zhipu": {"api_key": "k", "model": "m"}}}
        with patch.dict(tr.ENGINES["google"], {"fn": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("被限流"))}), \
             patch.dict(tr.ENGINES["llm"], {"fn": lambda *a, **k: "备用译文"}):
            zh, engine = tr.translate_text("hello world.", {"engine": "google"}, llm_raw)
        self.assertEqual((zh, engine), ("备用译文", "llm"))

    def test_active_first_when_works(self):
        with patch.dict(tr.ENGINES["mymemory"], {"fn": lambda *a, **k: "mm 译文"}), \
             patch.dict(tr.ENGINES["llm"], {"fn": lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该被调"))}):
            zh, engine = tr.translate_text("hi.", {"engine": "mymemory"}, {})
        self.assertEqual((zh, engine), ("mm 译文", "mymemory"))

    def test_nothing_configured(self):
        with patch.object(tr, "engine_configured", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                tr.translate_text("x", {"engine": "deepl"}, {})
        self.assertIn("没有可用的翻译引擎", str(ctx.exception))

    def test_all_engines_fail_reports_reasons(self):
        def boom(*a, **k):
            raise RuntimeError("炸了")
        with patch.dict(tr.ENGINES["volc"], {"fn": boom}), \
             patch.dict(tr.ENGINES["tencent"], {"fn": boom}), \
             patch.dict(tr.ENGINES["google"], {"fn": boom}), \
             patch.dict(tr.ENGINES["mymemory"], {"fn": boom}):
            with self.assertRaises(RuntimeError) as ctx:
                tr.translate_text("hello.", {"engine": "volc"}, {})
        self.assertIn("全部翻译引擎失败", str(ctx.exception))

    def test_engine_configured(self):
        cfg = {"deepl_api_key": "k"}
        self.assertTrue(tr.engine_configured("deepl", cfg, {}))
        self.assertFalse(tr.engine_configured("baidu", cfg, {}))
        self.assertTrue(tr.engine_configured("google", cfg, {}))   # 免费引擎永远可用
        self.assertFalse(tr.engine_configured("llm", cfg, {}))     # 无 LLM key


class TestCache(unittest.TestCase):
    def test_round_trip_and_hit(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            calls = {"n": 0}

            def fake_google(*a, **k):
                calls["n"] += 1
                return "译文X"
            with patch.dict(tr.ENGINES["google"], {"fn": fake_google}):
                zh1, e1 = tr.cached_translate("some abstract text", {"engine": "google"}, {}, data_dir)
                zh2, e2 = tr.cached_translate("some abstract text", {"engine": "google"}, {}, data_dir)
            self.assertEqual((zh1, e1), ("译文X", "google"))
            self.assertEqual((zh2, e2), ("译文X", "cache"))
            self.assertEqual(calls["n"], 1)          # 第二次命中缓存不再翻译

    def test_cache_lookup_miss(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(tr.cache_lookup("nothing", Path(td)), "")


class TestFormatWithTranslation(unittest.TestCase):
    def test_bilingual_layout(self):
        from litzaobao.gui import format_paper_detail
        rec = {"title": "T", "source": "crossref", "journal": "Journal of Machine Learning Research",
               "journal_tier": 2, "published": "2026-10-16",
               "summary_zh": "三句话摘要。", "abstract": "English abstract."}
        text = format_paper_detail(rec, translation_zh="中文译文段。")
        self.assertIn("— 中文译文 —", text)
        self.assertIn("中文译文段。", text)
        self.assertIn("— 英文摘要 —", text)
        # 中文译文在英文摘要之前
        self.assertLess(text.index("中文译文段"), text.index("英文摘要"))


if __name__ == "__main__":
    unittest.main()
