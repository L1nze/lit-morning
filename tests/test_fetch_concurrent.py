"""并发抓取测试：多源并行执行（汇合点证明）、输出顺序与 jobs 定义顺序一致、错误格式不变。"""
import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from unittest.mock import patch

from litzaobao.config import DEFAULTS, Config, deep_merge
from litzaobao.digest import _fetch_all
from litzaobao.paper import Paper

CUTOFF = date.today() - timedelta(days=3)


def make_paper(pid):
    return Paper(id=pid, source="arxiv", title=f"paper {pid}", matched=["llm"])


def make_cfg(**sources):
    """默认开 arxiv/openalex/crossref、关 scopus（无 elsevier key 本也不会跑）。"""
    src = {"arxiv": True, "openalex": True, "crossref": True, "scopus": False}
    src.update(sources)
    return Config(raw=deep_merge(DEFAULTS, {"sources": src}))


class TestFetchConcurrent(unittest.TestCase):
    def test_output_order_follows_jobs_definition(self):
        """首个源最慢：若按完成顺序输出会排到后面，正确实现必须按 jobs 定义顺序。"""
        cfg = make_cfg()
        pa, pc = make_paper("arxiv:1"), make_paper("doi:10.1/x")

        def slow_arxiv(*a, **k):
            time.sleep(0.25)
            return [pa]

        def dead_openalex(*a, **k):
            raise RuntimeError("GET 失败: https://api.openalex.org/works :: HTTP 500")

        buf = io.StringIO()
        with patch("litzaobao.digest.arxiv_source.fetch", side_effect=slow_arxiv), \
             patch("litzaobao.digest.openalex_source.fetch", side_effect=dead_openalex), \
             patch("litzaobao.digest.crossref_source.fetch",
                   side_effect=lambda *a, **k: [pc]), \
             redirect_stdout(buf):
            papers, errors = _fetch_all(cfg, CUTOFF, CUTOFF)

        self.assertEqual([p.id for p in papers], ["arxiv:1", "doi:10.1/x"])  # 汇总顺序
        self.assertEqual(errors, ["OpenAlex 不可用(HTTP 500)"])              # 错误格式不变
        names = [ln.split(":")[0].strip() for ln in buf.getvalue().splitlines()
                 if "[抓取]" in ln]
        self.assertEqual(names, ["[抓取] arXiv", "[抓取] OpenAlex", "[抓取] Crossref"])

    def test_sources_run_in_parallel(self):
        """两个源在汇合点会合：若被串行执行，第一个 wait 超时破裂 → 该源失败。"""
        cfg = make_cfg(crossref=False)
        barrier = threading.Barrier(2, timeout=8)

        def meet(pid):
            barrier.wait()
            return [make_paper(pid)]

        with patch("litzaobao.digest.arxiv_source.fetch",
                   side_effect=lambda *a, **k: meet("arxiv:1")), \
             patch("litzaobao.digest.openalex_source.fetch",
                   side_effect=lambda *a, **k: meet("oa:1")), \
             redirect_stdout(io.StringIO()):
            papers, errors = _fetch_all(cfg, CUTOFF, CUTOFF)

        self.assertEqual(errors, [])                       # 没有源因等待而失败
        self.assertEqual(sorted(p.id for p in papers), ["arxiv:1", "oa:1"])

    def test_no_sources_ok(self):
        """全部数据源关闭：线程池按 min(6,0) or 1 创建但不提交任务，正常返回空。"""
        cfg = make_cfg(arxiv=False, openalex=False, crossref=False)
        with redirect_stdout(io.StringIO()) as buf:
            papers, errors = _fetch_all(cfg, CUTOFF, CUTOFF)
        self.assertEqual((papers, errors), ([], []))
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
