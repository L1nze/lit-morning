import unittest

from litzaobao.gui import format_paper_detail


def rec(**kw):
    base = {"id": "doi:10.1/x", "source": "crossref",
            "title": "Multi-agent evaluation of language models",
            "authors": ["Alice", "Bob"], "abstract": "English abstract here.",
            "url": "https://doi.org/10.1/x", "published": "2026-10-16",
            "journal": "Journal of Machine Learning Research", "journal_tier": 2, "score": 12,
            "matched": ["multi-agent", "llm"],
            "summary_zh": "中文摘要三句话。", "highlight": "智能体直接相关"}
    base.update(kw)
    return base


class TestFormatPaperDetail(unittest.TestCase):
    def test_full_record(self):
        text = format_paper_detail(rec(), liked=True)
        self.assertIn("Multi-agent evaluation of language models", text)
        self.assertIn("⭐ 权威期刊", text)          # tier 徽章
        self.assertIn("Journal of Machine Learning Research", text)
        self.assertIn("匹配分 12", text)
        self.assertIn("Alice 等 2 人", text)
        self.assertIn("❤ 已点赞", text)
        self.assertIn("⚡ 智能体直接相关", text)
        self.assertIn("中文摘要三句话。", text)
        self.assertIn("English abstract here.", text)
        self.assertIn("https://doi.org/10.1/x", text)

    def test_sparse_record_no_crash(self):
        text = format_paper_detail({"title": "Only title"}, liked=False)
        self.assertIn("Only title", text)
        self.assertNotIn("None", text)            # 缺字段不渲染 None

    def test_arxiv_label_and_no_badge(self):
        text = format_paper_detail(rec(source="arxiv", journal="", journal_tier=3))
        self.assertIn("arXiv 预印本", text)
        self.assertNotIn("⭐", text)
        self.assertNotIn("🏆", text)

    def test_journal_rank_line(self):
        text = format_paper_detail(rec(journal_rank={"cas": "1区", "jcr": "Q1", "imf": 12.3}))
        self.assertIn("📊", text)
        self.assertIn("中科院 1区", text)
        self.assertIn("JCR Q1", text)
        self.assertIn("IF 12.3", text)

    def test_no_rank_no_line(self):
        self.assertNotIn("📊", format_paper_detail(rec()))


if __name__ == "__main__":
    unittest.main()
