import unittest
from unittest.mock import patch

from litzaobao.paper import Paper
from litzaobao.summarize import enrich, extract_json, fallback_summary


def make_paper(pid="arxiv:1"):
    return Paper(id=pid, source="arxiv", title="LLM agent paper",
                 abstract="First sentence. Second sentence. Third.",
                 matched=["llm", "agent"])


FAKE_LLM_JSON = """```json
{"overview": "今日两篇，均与智能体相关。",
 "items": [{"id": "arxiv:1", "summary_zh": "句一。句二。句三。",
            "highlight": "智能体+大模型直接相关"}]}
```"""


class TestExtractJson(unittest.TestCase):
    def test_fenced(self):
        data = extract_json(FAKE_LLM_JSON)
        self.assertEqual(data["overview"], "今日两篇，均与智能体相关。")
        self.assertEqual(data["items"][0]["id"], "arxiv:1")

    def test_plain_with_noise(self):
        data = extract_json('好的，结果如下：\n{"overview": "x", "items": []}\n以上。')
        self.assertEqual(data["items"], [])

    def test_bad_raises(self):
        with self.assertRaises(ValueError):
            extract_json("完全不是 JSON")


class TestEnrich(unittest.TestCase):
    def test_no_key_fallback(self):
        papers = [make_paper()]
        overview = enrich(papers, {"api_key": "", "model": "m"})
        self.assertIn("未配置 LLM", overview)
        self.assertEqual(papers[0].summary_zh, "First sentence. Second sentence.")
        self.assertIn("llm", papers[0].highlight)

    def test_llm_success(self):
        from litzaobao import summarize
        with patch.object(summarize, "call_llm",
                          return_value=("总览OK", {"arxiv:1": {
                              "summary_zh": "句一。句二。句三。", "highlight": "强相关"}})):
            papers = [make_paper()]
            overview = enrich(papers, {"api_key": "k", "model": "m"})
        self.assertEqual(overview, "总览OK")
        self.assertEqual(papers[0].summary_zh, "句一。句二。句三。")

    def test_llm_failure_degrades(self):
        from litzaobao import summarize
        with patch.object(summarize, "call_llm", side_effect=RuntimeError("boom")):
            papers = [make_paper()]
            overview = enrich(papers, {"api_key": "k", "model": "m"})
        self.assertIn("降级", overview)
        self.assertTrue(papers[0].summary_zh)

    def test_fallback_truncates(self):
        p = make_paper()
        p.abstract = "Word " * 200
        self.assertTrue(fallback_summary(p).endswith("…"))


if __name__ == "__main__":
    unittest.main()
