import unittest
from datetime import date

from litzaobao.paper import Paper
from litzaobao.render import render_html, render_markdown
from litzaobao.summarize import fallback_summary


def make_paper():
    return Paper(
        id="arxiv:1", source="arxiv",
        title="Agent <b>Reasoning</b> Benchmark & Safety",
        abstract="Long abstract. " * 50, url="https://arxiv.org/abs/1",
        published="2026-10-17", authors=["Alice", "Bob", "Carol"],
        score=8, matched=["agent", "reasoning"], journal="arXiv 预印本",
        summary_zh="中文摘要测试。", highlight="命中方法词: reasoning")


class TestMarkdown(unittest.TestCase):
    def test_contains_link_and_summary(self):
        md = render_markdown([make_paper()], "总览文本", __import__("datetime").date(2026, 10, 17))
        self.assertIn("[Agent <b>Reasoning</b> Benchmark & Safety](https://arxiv.org/abs/1)", md)
        self.assertIn("总览文本", md)
        self.assertIn("中文摘要测试。", md)
        self.assertIn("Alice 等 3 人", md)


class TestHtml(unittest.TestCase):
    def test_escapes_html(self):
        html = render_html([make_paper()], "总览", date(2026, 10, 17))
        self.assertNotIn("<b>Reasoning</b>", html)          # 原样标签被转义
        self.assertIn("&lt;b&gt;<mark>Reasoning</mark>&lt;/b&gt;", html)  # 转义后命中词加 mark
        self.assertIn("https://arxiv.org/abs/1", html)

    def test_overview_and_tags(self):
        html = render_html([make_paper()], "总览X", date(2026, 10, 17))
        self.assertIn("总览X", html)
        self.assertIn('class="tag"', html)


class TestHighlight(unittest.TestCase):
    def test_title_mark_keeps_original_case(self):
        html = render_html([make_paper()], "总览", date(2026, 10, 17))
        # 包住原文片段、保留原大小写，而不是替换成小写关键词
        self.assertIn("<mark>Agent</mark>", html)
        self.assertIn("<mark>Reasoning</mark>", html)
        self.assertNotIn("<mark>agent</mark>", html)   # 摘要里没有该词

    def test_abstract_marked_summary_not(self):
        p = make_paper()
        p.abstract = "The agent prototypes were tested with reasoning probes."
        p.summary_zh = "本文研究 agent 与 reasoning 评估。"
        html = render_html([p], "总览", date(2026, 10, 17))
        self.assertIn("<mark>agent</mark> prototypes", html)   # 英文摘要命中且词边界正确
        self.assertIn("<mark>reasoning</mark> probes", html)
        self.assertNotIn("<mark>agent</mark> 与", html)        # 中文摘要不加 mark
        self.assertNotIn("与 <mark>reasoning</mark>", html)

    def test_no_mark_when_matched_empty(self):
        p = make_paper()
        p.matched = []
        html = render_html([p], "总览", date(2026, 10, 17))
        self.assertNotIn("<mark>", html)

    def test_overlapping_terms_no_nested_marks(self):
        p = make_paper()
        p.title = "Agent agents models"
        p.abstract = ""
        p.matched = ["agent", "agents", "model"]
        html = render_html([p], "总览", date(2026, 10, 17))
        self.assertIn("<mark>Agent</mark>", html)
        self.assertIn("<mark>agents</mark>", html)       # 复数词形也命中
        self.assertIn("<mark>models</mark>", html)
        self.assertNotIn("<mark><mark>", html)           # 一次替换，不产生嵌套
        self.assertEqual(html.count("<mark>"), html.count("</mark>"))


class TestFallbackSummary(unittest.TestCase):
    def test_two_sentences_and_truncation(self):
        p = make_paper()
        p.abstract = "First sentence here. Second one follows. Third is cut."
        self.assertEqual(fallback_summary(p), "First sentence here. Second one follows.")
        p.abstract = "One very long sentence " * 40
        self.assertTrue(fallback_summary(p).endswith("…"))


if __name__ == "__main__":
    unittest.main()
