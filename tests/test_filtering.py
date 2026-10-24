import unittest

from litzaobao.filtering import dedup_papers, find_terms, make_pattern, score_paper
from litzaobao.paper import Paper

KW = {
    "topic_terms": ["large language model", "llm", "language model", "foundation model"],
    "method_terms": ["agent", "multi-agent", "tool use", "reasoning", "planning",
                     "reflection"],
    "bonus_terms": ["benchmark", "evaluation", "fine-tuning", "rlhf", "hallucination",
                    "chain-of-thought", "memory", "multimodal"],
}


class TestPattern(unittest.TestCase):
    def test_plural(self):
        pat = make_pattern("agent")
        self.assertTrue(pat.search("llm agents are great"))
        self.assertFalse(pat.search("agen"))
        # y 结尾词形：memory → memories
        pat2 = make_pattern("memory")
        self.assertTrue(pat2.search("agent memory and memories"))
        self.assertFalse(pat2.search("memor"))

    def test_short_term_word_boundary(self):
        # 短词只按词边界命中，不误伤更长的派生词（agent 不匹配 agentic）
        pat = make_pattern("agent")
        self.assertTrue(pat.search("estimate the agent online"))
        self.assertFalse(pat.search("the agentic workflows of models"))
        self.assertFalse(pat.search("agentivity"))


class TestScoring(unittest.TestCase):
    def _paper(self, title, abstract=""):
        return Paper(id="t:1", source="arxiv", title=title, abstract=abstract)

    def test_topic_and_method_pass(self):
        p = self._paper("Tool use agents for large language models",
                        "reasoning traces...")
        score_paper(p, KW, require_topic=True, min_score=5)
        self.assertGreaterEqual(p.score, 5)
        self.assertIn("tool use", p.matched)

    def test_method_only_rejected(self):
        p = self._paper("Reasoning about images", "planning in plates")
        score_paper(p, KW, require_topic=True, min_score=5)
        self.assertEqual(p.score, 0)

    def test_topic_only_below_min(self):
        # 仅命中 topic(3)，无方法词、无加分词 → 3 < 5
        p = self._paper("A language model study", "corpus scaling and tokenization")
        score_paper(p, KW, require_topic=True, min_score=5)
        self.assertEqual(p.score, 0)

    def test_require_off(self):
        p = self._paper("Reflection in mirrors", "")
        score_paper(p, KW, require_topic=False, min_score=2)
        self.assertEqual(p.score, 2)


class TestDedup(unittest.TestCase):
    def test_dedup_by_title_keeps_richer(self):
        a = Paper(id="arxiv:1", source="arxiv", title="LLM Agent Monitoring!",
                  abstract="")
        b = Paper(id="doi:10.1/x", source="crossref",
                  title="llm  agent  monitoring", abstract="full abstract here")
        out = dedup_papers([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].abstract, "full abstract here")

    def test_dedup_keeps_arxiv_when_equal(self):
        a = Paper(id="arxiv:1", source="arxiv", title="Same Title", abstract="abc")
        b = Paper(id="doi:10.1/x", source="crossref", title="Same Title", abstract="abc")
        out = dedup_papers([a, b])
        self.assertEqual(out[0].source, "arxiv")


class TestFindTerms(unittest.TestCase):
    def test_find(self):
        hits = find_terms("tool use and planning in an llm system", KW["method_terms"])
        self.assertEqual(sorted(hits), ["planning", "tool use"])


if __name__ == "__main__":
    unittest.main()
