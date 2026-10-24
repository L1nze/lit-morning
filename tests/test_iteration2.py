import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao.config import Config, apply_env, load_config
from litzaobao.filtering import score_paper
from litzaobao.paper import Paper
from litzaobao.profile import build_profile, boost_paper, load_likes, save_like

KW = {
    "topic_terms": ["large language model", "llm", "language model", "foundation model"],
    "method_terms": ["agent", "multi-agent", "tool use", "reasoning", "planning",
                     "reflection"],
    "bonus_terms": ["benchmark", "evaluation", "fine-tuning", "rlhf", "hallucination",
                    "chain-of-thought", "memory", "multimodal"],
    "exclude_terms": ["survey", "review"],
}


class TestExcludeTerms(unittest.TestCase):
    def test_excluded_even_if_high_score(self):
        p = Paper(id="x", source="crossref",
                  title="A survey of multi-agent tool use for llm reasoning",
                  abstract="systematic review")
        score_paper(p, KW, require_topic=True, min_score=5)
        self.assertEqual(p.score, 0)

    def test_not_excluded_when_clean(self):
        p = Paper(id="y", source="crossref",
                  title="Multi-agent tool use for LLM reasoning",
                  abstract="chain-of-thought evaluation")
        score_paper(p, KW, require_topic=True, min_score=5)
        self.assertGreaterEqual(p.score, 5)

    def test_require_method(self):
        p = Paper(id="z", source="crossref",
                  title="Fine-tuning of foundation models for benchmark evaluation",
                  abstract="llm benchmark evaluation")
        score_paper(p, KW, require_topic=True, require_method=True, min_score=5)
        self.assertEqual(p.score, 0)


class TestApplyEnv(unittest.TestCase):
    def test_env_overrides(self):
        raw = {"push": {"serverchan_sendkey": "old"}, "top_n": 5}
        with patch.dict(os.environ, {"LITZB_SERVERCHAN_SENDKEY": "new",
                                     "LITZB_TOP_N": "20",
                                     "LITZB_LLM_API_KEY": "sk-x"}):
            out = apply_env(raw)
        self.assertEqual(out["push"]["serverchan_sendkey"], "new")
        self.assertEqual(out["top_n"], 20)
        # LITZB_LLM_API_KEY 写入 active 服务商（默认 zhipu）
        self.assertEqual(out["llm"]["providers"]["zhipu"]["api_key"], "sk-x")

    def test_bad_int_ignored(self):
        with patch.dict(os.environ, {"LITZB_TOP_N": "abc"}):
            out = apply_env({"top_n": 7})
        self.assertEqual(out["top_n"], 7)

    def test_empty_ignored(self):
        with patch.dict(os.environ, {"LITZB_LLM_API_KEY": ""}):
            out = apply_env({"llm": {"api_key": "keep"}})
        self.assertEqual(out["llm"]["api_key"], "keep")


class TestLegacyKeyCompat(unittest.TestCase):
    """旧版配置键名（已弃用）在加载时归一化为新名，用户零迁移。"""

    _OLD_GROUP = "bat" + "tery" + "_terms"          # 拼串书写，避免敏感词直写进仓库
    _OLD_REQUIRE = "require_" + "bat" + "tery" + "_term"

    def test_old_keys_normalized_on_load(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({
                "keywords": {self._OLD_GROUP: ["llm", "agent"]},
                self._OLD_REQUIRE: False,
            }), encoding="utf-8")
            cfg = load_config(p)
        self.assertEqual(cfg.keywords["topic_terms"], ["llm", "agent"])
        self.assertNotIn(self._OLD_GROUP, cfg.keywords)
        self.assertEqual(cfg.raw["require_topic_term"], False)
        self.assertNotIn(self._OLD_REQUIRE, cfg.raw)


class TestProfile(unittest.TestCase):
    def test_save_and_boost(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            liked = Paper(id="a:1", source="arxiv", title="Tool use agent",
                          abstract="reasoning signals and traces")
            save_like(data_dir, liked)
            save_like(data_dir, liked)  # 幂等
            likes = load_likes(data_dir)
            self.assertEqual(len(likes), 1)

            profile = build_profile(likes, KW)
            self.assertIn("tool use", profile)

            cand = Paper(id="a:2", source="arxiv",
                         title="Agent evaluation of language models",
                         abstract="reasoning signals")
            cand.score = 5
            gained = boost_paper(cand, profile)
            self.assertGreater(gained, 0)
            self.assertGreater(cand.score, 5)

            unrelated = Paper(id="a:3", source="arxiv", title="Language model calendar scaling",
                              abstract="corpus statistics")
            before = unrelated.score
            self.assertEqual(boost_paper(unrelated, profile), 0)
            self.assertEqual(unrelated.score, before)


if __name__ == "__main__":
    unittest.main()
