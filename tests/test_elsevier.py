import json
import unittest
from datetime import date
from unittest.mock import patch

from litzaobao.enrich import enrich_abstracts
from litzaobao.paper import Paper
from litzaobao.sources.elsevier_source import _parse, fetch

SCOPUS_RESP = {
    "search-results": {
        "entry": [
            {"dc:title": "Multi-agent planning diagnostics",
             "dc:creator": "Smith A.", "dc:identifier": "SCOPUS_ID:85001",
             "prism:doi": "10.1016/j.aiagent.2026.01",
             "prism:publicationName": "Journal of Machine Learning Research",
             "prism:coverDate": "2026-10-16",
             "citedby-count": "2"},
            {"dc:title": "Old paper", "prism:coverDate": "2020-01-01",
             "prism:doi": "10.1/old", "dc:identifier": "SCOPUS_ID:1"},
            {"dc:title": "", "prism:coverDate": "2026-10-16"},   # 无标题丢弃
        ]
    }
}

KW = {"topic_terms": ["llm"], "method_terms": ["agent"],
      "bonus_terms": [], "exclude_terms": []}


class TestScopus(unittest.TestCase):
    def test_parse(self):
        papers = _parse(json.dumps(SCOPUS_RESP), date(2026, 10, 1))
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p.source, "scopus")
        self.assertEqual(p.journal, "Journal of Machine Learning Research")
        self.assertEqual(p.doi, "10.1016/j.aiagent.2026.01")
        self.assertEqual(p.url, "https://doi.org/10.1016/j.aiagent.2026.01")
        self.assertEqual(p.abstract, "")          # Scopus 无摘要，靠 enrich 补

    def test_no_key_silent_skip(self):
        with patch("litzaobao.sources.elsevier_source.http_get") as m:
            self.assertEqual(fetch(date(2026, 10, 1), KW, api_key=""), [])
        m.assert_not_called()


ELS_RESP = {"abstracts-retrieval-response": {
    "coredata": {"dc:description": "Elsevier filled abstract."}}}


class TestEnrichElsevierFallback(unittest.TestCase):
    def test_openalex_fails_elsevier_fills(self):
        p = Paper(id="doi:10.1/x", source="scopus", title="T", doi="10.1/x")

        def fake_get(url, params=None, headers=None, **kw):
            if "openalex" in url:
                raise RuntimeError("429")
            if "elsevier" in url:
                return json.dumps(ELS_RESP)
            raise AssertionError(url)

        with patch("litzaobao.enrich.http_get", side_effect=fake_get):
            filled = enrich_abstracts([p], elsevier_key="els-key")
        self.assertEqual(filled, 1)
        self.assertEqual(p.abstract, "Elsevier filled abstract.")

    def test_no_elsevier_key_no_calls(self):
        p = Paper(id="doi:10.1/x", source="scopus", title="T", doi="10.1/x")
        with patch("litzaobao.enrich.http_get",
                   return_value=json.dumps({"results": []})) as m:
            enrich_abstracts([p], elsevier_key="")
        for call in m.call_args_list:
            self.assertNotIn("elsevier", call.args[0])


if __name__ == "__main__":
    unittest.main()
