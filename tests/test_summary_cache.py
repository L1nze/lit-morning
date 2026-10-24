"""摘要缓存测试：命中不重调 LLM、部分命中只送未命中、总览指纹复用、降级结果不落缓存。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao import summarize
from litzaobao.paper import Paper
from litzaobao.summarize import enrich


def make_paper(pid):
    return Paper(id=pid, source="arxiv", title=f"LLM agent paper {pid}",
                 abstract="First sentence. Second sentence.",
                 matched=["llm", "agent"])


def make_llm_raw():
    return {"api_key": "k", "model": "m"}


class TestSummaryCache(unittest.TestCase):
    def test_second_call_skips_llm_and_keeps_summary(self):
        """同一批论文第二次 enrich 不再触发 LLM 调用，摘要与首次一致。"""
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "data" / "summary_cache.json"

            def fake_llm(batch, llm_raw, system_prompt=None):
                return (f"总览-{len(batch)}", {p.id: {"summary_zh": f"摘要-{p.id}",
                                                      "highlight": "强相关"} for p in batch})

            with patch.object(summarize, "call_llm", side_effect=fake_llm) as mock_llm:
                first = [make_paper("a:1"), make_paper("a:2")]
                ov1 = enrich(first, make_llm_raw(), cache_path=cache_path)
                self.assertEqual(mock_llm.call_count, 1)
                self.assertEqual(ov1, "总览-2")

                second = [make_paper("a:1"), make_paper("a:2")]   # 全新对象、同 id
                ov2 = enrich(second, make_llm_raw(), cache_path=cache_path)
                self.assertEqual(mock_llm.call_count, 1)           # 未再调 LLM
                self.assertEqual(ov2, "总览-2")                    # 总览按指纹复用
                self.assertEqual([p.summary_zh for p in second],
                                 ["摘要-a:1", "摘要-a:2"])         # 摘要一致

    def test_partial_hit_sends_only_misses(self):
        """部分命中时只有未命中的论文进入 LLM 请求。"""
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "summary_cache.json"
            cache_path.write_text(json.dumps(
                {"a:1": {"summary_zh": "旧摘要", "highlight": "旧点评"}}), encoding="utf-8")

            batches = []

            def fake_llm(batch, llm_raw, system_prompt=None):
                batches.append([p.id for p in batch])
                return ("总览", {"a:2": {"summary_zh": "新摘要", "highlight": "新点评"}})

            papers = [make_paper("a:1"), make_paper("a:2")]
            with patch.object(summarize, "call_llm", side_effect=fake_llm):
                overview = enrich(papers, make_llm_raw(), cache_path=cache_path)

            self.assertEqual(batches, [["a:2"]])          # 只送未命中的
            self.assertEqual(papers[0].summary_zh, "旧摘要")  # 命中直接回填
            self.assertEqual(papers[1].summary_zh, "新摘要")
            self.assertEqual(overview, "总览")
            # 新结果合并写回，不覆盖旧条目
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["a:1"]["summary_zh"], "旧摘要")
            self.assertEqual(saved["a:2"]["summary_zh"], "新摘要")

    def test_llm_failure_writes_nothing(self):
        """LLM 调用失败降级 → 降级结果不写入缓存。"""
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "summary_cache.json"
            with patch.object(summarize, "call_llm", side_effect=RuntimeError("boom")):
                papers = [make_paper("a:1")]
                overview = enrich(papers, make_llm_raw(), cache_path=cache_path)
            self.assertIn("降级", overview)
            self.assertFalse(cache_path.exists())          # 无缓存文件

            # 降级后再跑一次仍会尝试 LLM（缓存里没有假数据）
            with patch.object(summarize, "call_llm",
                              return_value=("总览", {"a:1": {"summary_zh": "真摘要",
                                                             "highlight": "h"}})):
                papers = [make_paper("a:1")]
                enrich(papers, make_llm_raw(), cache_path=cache_path)
            self.assertEqual(papers[0].summary_zh, "真摘要")

    def test_no_key_path_untouched_by_cache(self):
        """无 api_key 路径行为不变：规则摘要、不读不写缓存。"""
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "summary_cache.json"
            cache_path.write_text(json.dumps(
                {"a:1": {"summary_zh": "缓存摘要", "highlight": "h"}}), encoding="utf-8")
            papers = [make_paper("a:1")]
            overview = enrich(papers, {"api_key": "", "model": "m"}, cache_path=cache_path)
            self.assertIn("未配置 LLM", overview)
            self.assertEqual(papers[0].summary_zh, "First sentence. Second sentence.")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))
                             ["a:1"]["summary_zh"], "缓存摘要")   # 原样未动


if __name__ == "__main__":
    unittest.main()
