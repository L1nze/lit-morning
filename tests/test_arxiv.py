import unittest
from datetime import date

from litzaobao.sources.arxiv_source import build_query, parse_atom

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2610.12345v1</id>
    <published>2026-10-16T08:00:00Z</published>
    <updated>2026-10-16T08:00:00Z</updated>
    <title>Tool-Use Agents for
      Large Language Models</title>
    <summary>  We study reasoning  traces in agents.  </summary>
    <author><name>Alice  Smith</name></author>
    <author><name>Bob Li</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2601.00001v2</id>
    <published>2026-01-01T00:00:00Z</published>
    <title>Old paper on language models</title>
    <summary>ancient</summary>
    <author><name>Carol Wu</name></author>
  </entry>
</feed>"""


class TestArxiv(unittest.TestCase):
    def test_build_query(self):
        q = build_query({"topic_terms": ["llm", "language model"],
                         "method_terms": ["agent", "reasoning"]})
        self.assertIn('(all:"llm" OR all:"language model")', q)
        self.assertIn('(all:"agent" OR all:"reasoning")', q)
        self.assertIn(" AND ", q)

    def test_parse_and_date_filter(self):
        papers = parse_atom(ATOM)
        self.assertEqual(len(papers), 2)
        p = papers[0]
        self.assertEqual(p.id, "arxiv:2610.12345v1")
        self.assertEqual(p.title, "Tool-Use Agents for Large Language Models")
        self.assertEqual(p.authors, ["Alice Smith", "Bob Li"])
        self.assertTrue(p.abstract.startswith("We study reasoning"))
        self.assertEqual(p.url, "https://arxiv.org/abs/2610.12345v1")
        recent = [x for x in papers if x.published >= "2026-10-01"]
        self.assertEqual(len(recent), 1)


if __name__ == "__main__":
    unittest.main()
