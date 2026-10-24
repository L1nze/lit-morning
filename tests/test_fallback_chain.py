import unittest
from unittest.mock import patch

from litzaobao import llm_providers as lp


def raw_llm(fallback_model="", fallback_pid=""):
    providers = {"zhipu": {"api_key": "k1", "model": "glm-4-flash",
                           "fallback_model": fallback_model}}
    if fallback_pid == "zhipu":
        fallback_pid = ""   # 兜底服务不能是当前服务商本身场景按配置原样传入
    out = {"active": "zhipu", "providers": providers}
    if fallback_pid:
        providers[fallback_pid] = {"api_key": "k2", "model": "fb-model"}
        out["fallback"] = {"provider_id": fallback_pid}
    return out


class TestFallbackModel(unittest.TestCase):
    def test_resolve_includes_fallback_model(self):
        r = lp.resolve(raw_llm(fallback_model="glm-4-air"))
        self.assertEqual(r["fallback_model"], "glm-4-air")

    def test_apply_save_fallback_fields(self):
        cfgd = {}
        lp.apply_llm_save(cfgd, "zhipu", model="m", fallback_model="m2",
                          fallback_provider="deepseek")
        self.assertEqual(cfgd["llm"]["providers"]["zhipu"]["fallback_model"], "m2")
        self.assertEqual(cfgd["llm"]["fallback"], {"provider_id": "deepseek"})
        # 显式空串清除
        lp.apply_llm_save(cfgd, "zhipu", fallback_model="", fallback_provider="")
        self.assertEqual(cfgd["llm"]["providers"]["zhipu"]["fallback_model"], "")
        self.assertEqual(cfgd["llm"]["fallback"], {"provider_id": ""})


class TestFallbackProvider(unittest.TestCase):
    def test_resolve_fallback(self):
        fb = lp.resolve_fallback(raw_llm(fallback_pid="deepseek"))
        self.assertEqual(fb["provider_id"], "deepseek")
        self.assertEqual(fb["model"], "fb-model")
        self.assertEqual(fb["api_key"], "k2")

    def test_resolve_fallback_missing_or_unkeyed(self):
        self.assertIsNone(lp.resolve_fallback(raw_llm()))
        self.assertIsNone(lp.resolve_fallback({"fallback": {"provider_id": "ghost"}}))
        unkeyed = {"active": "zhipu",
                   "providers": {"deepseek": {"model": "m"}},   # 无 key
                   "fallback": {"provider_id": "deepseek"}}
        self.assertIsNone(lp.resolve_fallback(unkeyed))


class TestChatChain(unittest.TestCase):
    def _patch_post(self, outcomes):
        """outcomes: 按 (base_url 含某特征, model) 返回或抛错。"""
        calls = []

        def fake_post(url, payload, **kw):
            model = payload["model"]
            calls.append(model)
            action = outcomes.get(model, outcomes.get("*"))
            if isinstance(action, Exception):
                raise action
            return action
        return calls, fake_post

    OK = '{"choices": [{"message": {"content": "ok"}}]}'

    def test_primary_model_first(self):
        calls, fake = self._patch_post({"*": self.OK})
        with patch("litzaobao.llm_providers.http_post", side_effect=fake):
            resp = lp.chat_completion(raw_llm(fallback_model="glm-4-air",
                                              fallback_pid="deepseek"), {})
        self.assertEqual(resp["choices"][0]["message"]["content"], "ok")
        self.assertEqual(calls, ["glm-4-flash"])

    def test_fallback_model_then_provider(self):
        calls, fake = self._patch_post({
            "glm-4-flash": RuntimeError("限流"),
            "glm-4-air": RuntimeError("又挂"),
            "fb-model": self.OK})
        with patch("litzaobao.llm_providers.http_post", side_effect=fake):
            resp = lp.chat_completion(raw_llm(fallback_model="glm-4-air",
                                              fallback_pid="deepseek"), {})
        self.assertEqual(resp["choices"][0]["message"]["content"], "ok")
        self.assertEqual(calls, ["glm-4-flash", "glm-4-air", "fb-model"])

    def test_all_fail_raises_last(self):
        calls, fake = self._patch_post({
            "glm-4-flash": RuntimeError("err1"),
            "glm-4-air": RuntimeError("err2"),
            "fb-model": RuntimeError("err3")})
        with patch("litzaobao.llm_providers.http_post", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                lp.chat_completion(raw_llm(fallback_model="glm-4-air",
                                          fallback_pid="deepseek"), {})
        self.assertEqual(calls, ["glm-4-flash", "glm-4-air", "fb-model"])
        self.assertEqual(str(ctx.exception), "err3")


if __name__ == "__main__":
    unittest.main()
