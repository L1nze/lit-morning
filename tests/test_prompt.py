import json
import unittest
from unittest.mock import patch

from litzaobao import summarize
from litzaobao.paper import Paper
from litzaobao.summarize import (DEFAULT_FOCUS, default_system_prompt,
                                 effective_system_prompt, normalize_custom_prompt)


class TestPromptTemplate(unittest.TestCase):
    def test_default_contains_focus(self):
        text = default_system_prompt()
        self.assertIn(DEFAULT_FOCUS, text)
        self.assertIn("summary_zh", text)

    def test_custom_focus_swaps_domain(self):
        text = default_system_prompt("钙钛矿光伏器件稳定性")
        self.assertIn("钙钛矿光伏器件稳定性", text)
        self.assertNotIn(DEFAULT_FOCUS, text)

    def test_effective_priority(self):
        # 自定义 > 模板(focus) > 模板(默认)
        self.assertEqual(effective_system_prompt({"system_prompt": "自定义X"}, "某方向"), "自定义X")
        eff = effective_system_prompt({}, "钙钛矿")
        self.assertIn("钙钛矿", eff)
        self.assertEqual(effective_system_prompt({}, ""), default_system_prompt())

    def test_normalize_for_save(self):
        focus = "某方向"
        self.assertEqual(normalize_custom_prompt("", focus), "")
        self.assertEqual(normalize_custom_prompt(default_system_prompt(focus), focus), "")
        self.assertEqual(normalize_custom_prompt("我的手写版", focus), "我的手写版")

    def test_enrich_uses_focus_in_system_message(self):
        captured = {}

        def fake_post(url, payload, **kw):
            captured.update(payload)
            return json.dumps({"choices": [{"message": {"content":
                '{"overview": "ok", "items": []}'}}]})

        papers = [Paper(id="a:1", source="arxiv", title="T", abstract="A")]
        llm_raw = {"active": "zhipu", "providers": {"zhipu": {"api_key": "k", "model": "m"}}}
        with patch("litzaobao.llm_providers.http_post", side_effect=fake_post):
            summarize.enrich(papers, llm_raw, research_focus="多智能体协作")
        sys_msg = captured["messages"][0]["content"]
        self.assertIn("多智能体协作", sys_msg)
        self.assertNotIn(DEFAULT_FOCUS, sys_msg)


class TestRenderTagline(unittest.TestCase):
    def test_tagline_override(self):
        import datetime

        from litzaobao.render import DEFAULT_TAGLINE, render_markdown
        p = Paper(id="x:1", source="arxiv", title="T", url="https://x",
                  published="2026-10-20", score=5)
        md = render_markdown([p], "总览", datetime.date(2026, 10, 22),
                             tagline="钙钛矿光伏")
        self.assertIn("「钙钛矿光伏」", md)
        self.assertNotIn(DEFAULT_TAGLINE, md)


if __name__ == "__main__":
    unittest.main()
