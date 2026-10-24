import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao import llm_providers as lp
from litzaobao.config import apply_env


class TestResolve(unittest.TestCase):
    def test_default_falls_to_zhipu_preset(self):
        r = lp.resolve({})
        self.assertEqual(r["provider_id"], "zhipu")
        self.assertEqual(r["name"], "智谱 GLM")
        self.assertEqual(r["model"], "glm-4-flash")
        self.assertEqual(r["api_key"], "")

    def test_legacy_flat_folds_into_custom(self):
        raw = {"base_url": "https://my-llm.example/v1", "api_key": "sk-old", "model": "my-model"}
        r = lp.resolve(raw)
        self.assertEqual(r["provider_id"], "custom")
        self.assertEqual(r["base_url"], "https://my-llm.example/v1")
        self.assertEqual(r["api_key"], "sk-old")
        self.assertEqual(r["model"], "my-model")

    def test_partial_merge_with_preset(self):
        raw = {"active": "zhipu", "providers": {"zhipu": {"api_key": "k1", "model": "glm-4-air"}}}
        r = lp.resolve(raw)
        self.assertEqual(r["base_url"], "https://open.bigmodel.cn/api/paas/v4")  # 预设补全
        self.assertEqual(r["api_key"], "k1")
        self.assertEqual(r["model"], "glm-4-air")

    def test_user_defined_provider_passthrough(self):
        raw = {"active": "my-company",
               "providers": {"my-company": {"name": "公司网关",
                                            "base_url": "https://llm.corp/v1",
                                            "api_key": "c1", "model": "corp-model"}}}
        r = lp.resolve(raw)
        self.assertEqual(r["name"], "公司网关")
        self.assertEqual(r["base_url"], "https://llm.corp/v1")

    def test_bad_active_falls_back(self):
        r = lp.resolve({"active": "not-exist", "providers": {}})
        self.assertEqual(r["provider_id"], "zhipu")

    def test_system_prompt_passthrough(self):
        r = lp.resolve({"system_prompt": "  自定义提示词  "})
        self.assertEqual(r["system_prompt"], "自定义提示词")

    def test_handwritten_model_enters_list(self):
        provs = lp.all_providers({"providers": {"zhipu": {"model": "glm-x-experimental"}}})
        self.assertIn("glm-x-experimental", provs["zhipu"]["models"])


class TestStatus(unittest.TestCase):
    def test_status(self):
        base = {"base_url": "https://x", "api_key": "k", "model": "m", "key_required": True}
        self.assertEqual(lp.provider_status(base), "ready")
        self.assertEqual(lp.provider_status({**base, "api_key": ""}), "partial")
        self.assertEqual(lp.provider_status({**base, "model": ""}), "partial")
        self.assertEqual(lp.provider_status({**base, "base_url": ""}), "empty")
        # ollama 免 key
        self.assertEqual(lp.provider_status({"base_url": "http://localhost:11434/v1",
                                             "api_key": "", "model": "qwen2.5:7b",
                                             "key_required": False}), "ready")


class TestWriteActive(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"llm": {"active": "zhipu", "providers": {}}}),
                            encoding="utf-8")
            lp.write_active(path, "deepseek", "deepseek-reasoner")
            cfg = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["llm"]["active"], "deepseek")
            self.assertEqual(cfg["llm"]["providers"]["deepseek"]["model"], "deepseek-reasoner")
            # 其他配置不动
            self.assertIn("zhipu", lp.all_providers(cfg["llm"]))

    def test_missing_config_exits(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                lp.write_active(Path(td) / "nope.json", "zhipu")


class TestEnvOverrides(unittest.TestCase):
    def test_llm_env_writes_active_provider(self):
        with patch.dict(os.environ, {"LITZB_LLM_ACTIVE": "deepseek",
                                     "LITZB_LLM_API_KEY": "sk-ds",
                                     "LITZB_LLM_MODEL": "deepseek-reasoner"}):
            out = apply_env({})
        self.assertEqual(out["llm"]["active"], "deepseek")
        self.assertEqual(out["llm"]["providers"]["deepseek"]["api_key"], "sk-ds")
        self.assertEqual(out["llm"]["providers"]["deepseek"]["model"], "deepseek-reasoner")
        # resolve 能与预设合并出完整端点
        r = lp.resolve(out["llm"])
        self.assertEqual(r["base_url"], "https://api.deepseek.com/v1")

    def test_llm_env_into_default_zhipu(self):
        with patch.dict(os.environ, {"LITZB_LLM_API_KEY": "sk-glm"}, clear=False):
            os.environ.pop("LITZB_LLM_ACTIVE", None)
            out = apply_env({})
        self.assertEqual(out["llm"]["providers"]["zhipu"]["api_key"], "sk-glm")


class TestSystemPromptUsed(unittest.TestCase):
    def test_call_llm_uses_default_prompt(self):
        from litzaobao.paper import Paper
        from litzaobao.summarize import DEFAULT_FOCUS, DEFAULT_SYSTEM_PROMPT, call_llm

        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"choices": [{"message": {"content":
                '{"overview": "ok", "items": []}'}}]})

        p = Paper(id="a:1", source="arxiv", title="T", abstract="A")
        with patch("litzaobao.llm_providers.http_post", side_effect=fake_post):
            call_llm([p], {"base_url": "https://x/v1", "api_key": "k",
                           "model": "m", "system_prompt": ""})
        sys_msg = captured["messages"][0]["content"]
        self.assertEqual(sys_msg, DEFAULT_SYSTEM_PROMPT)
        self.assertIn(DEFAULT_FOCUS, sys_msg)

    def test_call_llm_custom_prompt_overrides(self):
        from litzaobao.paper import Paper
        from litzaobao.summarize import call_llm
        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"choices": [{"message": {"content":
                '{"overview": "ok", "items": []}'}}]})

        p = Paper(id="a:1", source="arxiv", title="T", abstract="A")
        with patch("litzaobao.llm_providers.http_post", side_effect=fake_post):
            call_llm([p], {"base_url": "https://x/v1", "api_key": "k",
                           "model": "m", "system_prompt": "你是猫娘"})
        self.assertEqual(captured["messages"][0]["content"], "你是猫娘")


if __name__ == "__main__":
    unittest.main()
