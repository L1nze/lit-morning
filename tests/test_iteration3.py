import json
import unittest
from unittest.mock import patch

from litzaobao.enrich import enrich_abstracts
from litzaobao.paper import Paper
from litzaobao.sources.crossref_source import _parse

CROSSREF_JSON = {
    "message": {"items": [
        {
            "DOI": "10.1000/aaa",
            "title": ["LLM agent paper"],
            "author": [{"family": "Smith", "given": "Alice"}],
            "abstract": "<p>We study <italic>agent</italic> cells.</p>",
            "issued": {"date-parts": [[2026, 12, 1]]},
            "published-online": {"date-parts": [[2026, 10, 15]]},
            "URL": "https://doi.org/10.1000/aaa",
            "container-title": ["Journal of Machine Learning Research"],
        },
        {
            "DOI": "10.1000/bbb",
            "title": ["No online date paper"],
            "issued": {"date-parts": [[2026, 10, 10]]},
            "URL": "https://doi.org/10.1000/bbb",
        },
        {
            "DOI": "10.1000/ccc",
            "title": ["Future issue date paper"],
            "issued": {"date-parts": [[2026, 12, 1]]},
            "created": {"date-parts": [[2026, 10, 14]]},
            "URL": "https://doi.org/10.1000/ccc",
        },
    ]}
}


class TestCrossrefParse(unittest.TestCase):
    def test_prefers_published_online_and_cleans_jats(self):
        papers = _parse(json.dumps(CROSSREF_JSON))
        self.assertEqual(len(papers), 3)
        a = next(p for p in papers if p.doi == "10.1000/aaa")
        self.assertEqual(a.published, "2026-10-15")       # 在线日期优先于刊期 2026-12
        self.assertEqual(a.abstract, "We study agent cells.")  # JATS 标签已剥离
        self.assertNotIn("<", a.abstract)
        b = next(p for p in papers if p.doi == "10.1000/bbb")
        self.assertEqual(b.published, "2026-10-10")       # 无在线日期回退 issued
        c = next(p for p in papers if p.doi == "10.1000/ccc")
        self.assertEqual(c.published, "2026-10-14")       # 未来刊期时用 created 入库日


OPENALEX_RESP = {
    "results": [
        {"doi": "https://doi.org/10.1000/aaa",
         "abstract_inverted_index": {"Filled": [0], "abstract": [1]}},
    ]
}


class TestEnrich(unittest.TestCase):
    def test_enrich_fills_by_doi(self):
        p1 = Paper(id="doi:10.1000/aaa", source="crossref", title="A", doi="10.1000/aaa")
        p2 = Paper(id="doi:10.1000/zzz", source="crossref", title="B", doi="10.1000/zzz",
                   abstract="already")
        p3 = Paper(id="doi:10.1000/nohit", source="crossref", title="C", doi="10.1000/nohit")
        with patch("litzaobao.enrich.http_get",
                   return_value=json.dumps(OPENALEX_RESP)) as mock_get:
            filled = enrich_abstracts([p1, p2, p3])
        self.assertEqual(filled, 1)
        self.assertEqual(p1.abstract, "Filled abstract")
        self.assertEqual(p2.abstract, "already")
        self.assertEqual(p3.abstract, "")
        url = mock_get.call_args.args[0]
        self.assertIn("doi:", mock_get.call_args.kwargs["params"]["filter"])
        self.assertIn("10.1000/aaa", mock_get.call_args.kwargs["params"]["filter"])

    def test_enrich_silent_on_failure(self):
        p = Paper(id="doi:10.1/x", source="crossref", title="X", doi="10.1/x")
        with patch("litzaobao.enrich.http_get", side_effect=RuntimeError("429")):
            filled = enrich_abstracts([p])
        self.assertEqual(filled, 0)

    def test_enrich_skips_when_no_doi_or_has_abstract(self):
        no_doi = Paper(id="arxiv:1", source="arxiv", title="T", abstract="")
        has_abs = Paper(id="doi:10.1/y", source="crossref", title="T2",
                        doi="10.1/y", abstract="ok")
        with patch("litzaobao.enrich.http_get") as mock_get:
            self.assertEqual(enrich_abstracts([no_doi, has_abs]), 0)
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
