"""管道级冒烟测试：全部数据源打桩，验证
1) 全流程跑通不挂起（卡死回归护栏）；2) 连续两轮幂等（已读去重）；
3) 期刊分级：门槛过滤与顶刊排序加分生效；4) 落盘文件齐全。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from litzaobao.config import DEFAULTS, Config, deep_merge
from litzaobao.digest import run_digest
from litzaobao.paper import Paper


def make_papers():
    return [
        Paper(id="doi:10.1/jmlr", source="crossref",
              title="Multi-agent planning for language model evaluation",
              abstract="llm agent reasoning benchmark",
              url="https://doi.org/10.1/jmlr", published="2026-10-16",
              doi="10.1/jmlr", journal="Journal of Machine Learning Research"),
        Paper(id="doi:10.1/generic", source="crossref",
              title="Fine-tuning and evaluation of language models",
              abstract="llm benchmark and memory studies",
              url="https://doi.org/10.1/generic", published="2026-10-16",
              doi="10.1/generic", journal="Journal of Generic Studies"),
        Paper(id="doi:10.1/noise", source="crossref",
              title="A survey of benchmark suites for language models",
              abstract="systematic review of evaluation protocols",
              url="https://doi.org/10.1/noise", published="2026-10-16",
              doi="10.1/noise", journal="Journal of Generic Studies"),
        Paper(id="arxiv:2610.9", source="arxiv",
              title="Tool use evaluation for language models",
              abstract="agent benchmark for llm systems",
              url="https://arxiv.org/abs/2610.9", published="2026-10-12"),
    ]


def make_cfg(tmp: Path, **over) -> Config:
    raw = deep_merge(DEFAULTS, {
        "output_dir": str(tmp / "out"),
        "data_dir": str(tmp / "data"),
        "push": {"enabled": False, "provider": "none"},
        "llm": {"api_key": ""},
        **over,
    })
    return Config(raw=raw)


class TestPipelineSmoke(unittest.TestCase):
    def test_full_pipeline_idempotent_and_tiered(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp, top_n=10)
            papers = make_papers()
            day = date.today()
            with patch("litzaobao.digest.arxiv_source.fetch", return_value=papers), \
                 patch("litzaobao.digest.crossref_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.openalex_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.enrich_mod.enrich_abstracts", return_value=0):
                r1 = run_digest(cfg, no_push=True)
                # 幂等：第二轮同样数据应全部视为已读
                r2 = run_digest(cfg, no_push=True)

            # 综述论文被负向词剔除；LLM 微调论文(无方法词, require_method=false)保留；
            # arXiv 预印本保留 → 3 篇
            self.assertEqual(r1["count"], 3)
            self.assertEqual(r2["count"], 0)

            tiers = {p.title[:20]: p.journal_tier for p in r1["papers"]}
            jmlr = next(p for p in r1["papers"] if "Multi-agent planning" in p.title)
            self.assertEqual(jmlr.journal_tier, 2)
            self.assertGreater(jmlr.score, 5 + 2)  # 基础分 + 权威期刊加分

            out = tmp / "out"
            for ext in ("md", "html", "json"):
                self.assertTrue((out / f"digest-{day.isoformat()}.{ext}").exists())
            md = (out / f"digest-{day.isoformat()}.md").read_text(encoding="utf-8")
            self.assertIn("⭐ 权威期刊", md)      # 徽章已渲染
            self.assertNotIn("A survey of benchmark suites", md)  # 综述论文不出现

            # 资源泄漏硬证据：Windows 下若 SQLite 连接未关，目录将无法删除
            import shutil
            shutil.rmtree(tmp / "data")

    def test_min_journal_tier_filters_but_keeps_arxiv(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp, top_n=10, min_journal_tier=2)
            with patch("litzaobao.digest.arxiv_source.fetch", return_value=make_papers()), \
                 patch("litzaobao.digest.crossref_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.openalex_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.enrich_mod.enrich_abstracts", return_value=0):
                r = run_digest(cfg, no_push=True)
            titles = [p.title for p in r["papers"]]
            self.assertTrue(any("Tool use evaluation" in t for t in titles))  # arXiv 豁免
            self.assertFalse(any("Fine-tuning" in t for t in titles))        # tier3 期刊被滤
            self.assertTrue(any("Multi-agent planning" in t for t in titles))  # tier2 保留


if __name__ == "__main__":
    unittest.main()
