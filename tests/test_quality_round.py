import json
import unittest
from unittest.mock import patch

from litzaobao.journals import badge, journal_tier
from litzaobao.push.serverchan import push as sc_push
from litzaobao.render import render_markdown
from litzaobao.web import redact_url


class TestJournalTiers(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(journal_tier("Journal of Machine Learning-Research"), 2)  # 标点归一
        self.assertEqual(journal_tier("nature MACHINE intelligence"), 1)           # 大小写归一
        self.assertEqual(journal_tier("Nature Communications"), 1)
        self.assertEqual(journal_tier("IEEE Transactions on Pattern Analysis and Machine Intelligence"), 2)
        self.assertEqual(journal_tier("Artificial Intelligence"), 2)

    def test_unknown_and_empty(self):
        self.assertEqual(journal_tier("Some Random Journal"), 3)
        self.assertEqual(journal_tier(""), 3)

    def test_override(self):
        self.assertEqual(journal_tier("Some Random Journal", {"some random journal": 1}), 1)

    def test_badges(self):
        self.assertEqual(badge(1), "🏆 顶刊")
        self.assertEqual(badge(2), "⭐ 权威期刊")
        self.assertEqual(badge(3), "")


class TestSecurity(unittest.TestCase):
    def test_redact_url(self):
        url = "https://api.openalex.org/works?api_key=sk-SECRET123&q=agent"
        red = redact_url(url)
        self.assertNotIn("sk-SECRET123", red)
        self.assertIn("api_key=***", red)

    def test_redact_sendkey(self):
        red = redact_url("https://x.com/SCTabc123.send?token=tok9")
        self.assertNotIn("tok9", red)

    def test_serverchan_rejects_dirty_key(self):
        ok, msg = sc_push("SCT123/../../evil", "t", "d")
        self.assertFalse(ok)
        self.assertIn("非法字符", msg)


class TestMarkdownInjection(unittest.TestCase):
    def test_title_brackets_escaped(self):
        import datetime

        from litzaobao.paper import Paper
        p = Paper(id="x:1", source="arxiv",
                  title="Evil [click](https://evil.example) agent reasoning",
                  abstract="", url="https://arxiv.org/abs/1", published="2026-10-17")
        md = render_markdown([p], "ov", datetime.date(2026, 10, 17))
        self.assertIn("\\[click\\]", md)
        self.assertNotIn("[click](https://evil.example)", md)


S2_RESP = {
    "data": [
        {"paperId": "abc123", "title": "LLM agent benchmark study",
         "abstract": "We use multi-agent planning.", "venue": "Journal of Machine Learning Research",
         "publicationDate": "2026-10-16",
         "externalIds": {"DOI": "10.1000/s2"}, "authors": [{"name": "A B"}]},
        {"paperId": "", "title": "broken record", "publicationDate": "2026-10-16"},
    ], "total": 2}


if __name__ == "__main__":
    unittest.main()
