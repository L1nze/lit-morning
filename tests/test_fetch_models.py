import json
import unittest
from unittest.mock import patch

from litzaobao.llm_providers import apply_llm_save, fetch_models

MODELS_RESP = {"object": "list", "data": [
    {"id": "deepseek/deepseek-flash"}, {"id": "gpt-4o-mini"}, {"id": "qwen3:8b"}]}


class TestFetchModels(unittest.TestCase):
    def test_parse_and_sort(self):
        captured = {}

        def fake_get(url, timeout=20, retries=1, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return json.dumps(MODELS_RESP)
        # fetch_models 内延迟导入 litzaobao.web.http_get，patch 模块属性即可拦截
        with patch("litzaobao.web.http_get", side_effect=fake_get):
            models = fetch_models("https://gw.example/v1/", "sk-x")
        self.assertEqual(models, ["deepseek/deepseek-flash", "gpt-4o-mini", "qwen3:8b"])
        self.assertEqual(captured["url"], "https://gw.example/v1/models")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-x")

    def test_empty_raises(self):
        with patch("litzaobao.web.http_get", return_value=json.dumps({"data": []})):
            with self.assertRaises(RuntimeError):
                fetch_models("https://gw.example/v1", "")

    def test_apply_save_persists_models(self):
        cfgd = {}
        apply_llm_save(cfgd, "custom", model="a", models=["a", "b", "c"])
        self.assertEqual(cfgd["llm"]["providers"]["custom"]["models"], ["a", "b", "c"])
        # 不传 models 不动旧清单
        apply_llm_save(cfgd, "custom", model="b")
        self.assertEqual(cfgd["llm"]["providers"]["custom"]["models"], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
