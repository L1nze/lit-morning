"""全环节压测套件：并发 / 洪峰 / 畸形输入 / 故障注入。

断言策略：正确性硬断言；耗时用宽松上界（CI 机器差异大，只防数量级退化）。
"""
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest.mock import patch

from litzaobao import filtering, render, summarize, translate as tr_mod
from litzaobao.config import update_config_file
from litzaobao.digest import run_digest
from litzaobao.gui import UiQueue
from litzaobao.llm_providers import chat_completion
from litzaobao.paper import Paper
from litzaobao.store import SeenStore

KW = {"topic_terms": ["llm", "language model"], "method_terms": ["agent", "reasoning"],
      "bonus_terms": ["benchmark"], "exclude_terms": ["survey"]}


def make_paper(i: int, **over) -> Paper:
    p = Paper(id=f"doi:10.1/{i}", source="crossref",
              title=f"LLM agent study number {i} <tag> & 'quote'",
              authors=[f"Author {i}"], abstract="A" * 300 + f" study {i}.",
              url=f"https://doi.org/10.1/{i}", published="2026-10-20",
              doi=f"10.1/{i}", journal="Journal of Machine Learning Research" if i % 2 else "Random J")
    for k, v in over.items():
        setattr(p, k, v)
    return p


class TestPipelineThroughput(unittest.TestCase):
    """千篇级候选：打分→去重→已读过滤→渲染，正确性 + 宽松耗时上界。"""

    def test_thousand_papers_pipeline(self):
        papers = [make_paper(i) for i in range(1000)]
        papers += [make_paper(i) for i in range(500)]        # 500 条重复（跨源同文）
        t0 = time.perf_counter()
        for p in papers:
            filtering.score_paper(p, KW, require_topic=True, min_score=5)
        papers = [p for p in papers if p.score > 0]
        papers = filtering.dedup_papers(papers)
        t1 = time.perf_counter()
        self.assertEqual(len(papers), 1000)                   # 重复全去
        self.assertLess(t1 - t0, 5.0)                        # 数量级护栏

        html = render.render_html(papers[:100], "总览", date(2026, 10, 22))
        self.assertNotIn("<tag>", html)
        self.assertLess(len(html), 5_000_000)

    def test_seen_store_ten_thousand(self):
        with tempfile.TemporaryDirectory() as td:
            with SeenStore(Path(td) / "s.db") as store:
                t0 = time.perf_counter()
                store.mark_seen([make_paper(i) for i in range(10_000)], date.today())
                t1 = time.perf_counter()
                self.assertEqual(len(store.known_keys()), 10_000)
                fresh = store.filter_new([make_paper(1), make_paper(99_999)])
                t2 = time.perf_counter()
        self.assertEqual([p.id for p in fresh], ["doi:10.1/99999"])
        self.assertLess(t1 - t0, 10.0)
        self.assertLess(t2 - t1, 5.0)

    def test_pathological_inputs(self):
        weird = [
            make_paper(1, title="", abstract=""),                       # 全空
            make_paper(2, title="控制\x00字符\n换行", abstract=""),       # 控制字符
            make_paper(3, title="🌍" * 500, abstract="emoji" * 2000),   # 超长 emoji
            make_paper(4, title="日" * 3000, abstract="中" * 5000),      # 超长中文
            make_paper(5, title="[链接](https://evil)", abstract="<script>alert(1)</script>"),
        ]
        for p in weird:
            filtering.score_paper(p, KW, require_topic=False, min_score=0)
        out = render.render_html(weird, "ov", date(2026, 10, 22))
        self.assertNotIn("<script>", out)
        md = render.render_markdown(weird, "ov", date(2026, 10, 22))
        self.assertNotIn("[链接](https://evil)", md)
        # dedup_key 对空标题回退 id，不炸
        filtering.dedup_papers(weird + [make_paper(1, title="", abstract="")])


class TestLlmChainFaultInjection(unittest.TestCase):
    """兜底链随机故障注入 + 畸形响应。"""

    OK = '{"choices": [{"message": {"content": "ok"}}]}'

    def _raw(self, fb_model="fb-m", fb_pid="deepseek"):
        return {"active": "zhipu",
                "providers": {"zhipu": {"api_key": "k", "model": "m",
                                        "fallback_model": fb_model},
                              "deepseek": {"api_key": "k2", "model": fb_pid and "ds-m"}},
                **({"fallback": {"provider_id": fb_pid}} if fb_pid else {})}

    def test_repeated_failures_fall_through(self):
        """主模型连续失败 50 轮，每轮都应穿透到可用级且最终成功。"""
        import random
        rng = random.Random(42)

        def flaky_post(url, payload, **kw):
            model = payload["model"]
            if model == "m" or (model == "fb-m" and rng.random() < 0.5):
                raise RuntimeError("injected failure")
            return self.OK
        with patch("litzaobao.llm_providers.http_post", side_effect=flaky_post):
            for _ in range(50):
                resp = chat_completion(self._raw(), {})
                self.assertEqual(resp["choices"][0]["message"]["content"], "ok")

    def test_all_levels_fail_raises(self):
        def dead(url, payload, **kw):
            raise RuntimeError("dead")
        with patch("litzaobao.llm_providers.http_post", side_effect=dead):
            with self.assertRaises(RuntimeError):
                chat_completion(self._raw(), {})

    def test_malformed_llm_jsons(self):
        bad = ["", "```json\n{no", '{"overview": 1}']
        for i, text in enumerate(bad):
            with self.assertRaises(ValueError, msg=f"case {i}"):
                summarize.extract_json(text)
        # 宽容解析组（前后噪声剥离后合法）
        self.assertEqual(summarize.extract_json(
            '前言{"overview":"x","items":[]}尾巴')["overview"], "x")
        self.assertEqual(summarize.extract_json(
            '{"overview":"ok","items":[]}')["overview"], "ok")

    def test_giant_listing_truncated(self):
        papers = [make_paper(i, abstract="B" * 10_000) for i in range(60)]
        listing = summarize._listing(papers)
        # 每篇摘要截到 900，总长受控
        self.assertLess(len(listing), 60 * 1200)

    def test_translate_huge_text_chunking(self):
        text = "Sentence one is fine. " * 4000          # ~96KB
        chunks = tr_mod.split_chunks(text, 1400)
        self.assertGreater(len(chunks), 50)
        self.assertTrue(all(len(c) <= 1400 for c in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))


class TestConfigConcurrentWrites(unittest.TestCase):
    def test_ten_threads_update_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text("{}", encoding="utf-8")

            def worker(n):
                for i in range(20):
                    update_config_file(path, lambda c, n=n: c.update({f"key_{n}": i}))
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len([k for k in data if k.startswith("key_")]), 10)
            # 无残留 tmp 文件
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


class TestUiQueueFlood(unittest.TestCase):
    def test_thousand_callbacks(self):
        q = UiQueue()
        hits = []
        for i in range(1000):
            q.put(lambda i=i: hits.append(i))
        for fn in q.drain():
            fn()
        self.assertEqual(hits, list(range(1000)))


class TestWebConsoleStress(unittest.TestCase):
    """50 并发 + 畸形请求。"""

    @classmethod
    def setUpClass(cls):
        from litzaobao.config import DEFAULTS, Config, deep_merge
        from litzaobao.server import make_server
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "out").mkdir()
        cls.cfg_path = tmp / "config.json"
        cls.cfg_path.write_text("{}", encoding="utf-8")
        cls.server = make_server(Config(raw=deep_merge(DEFAULTS, {
            "output_dir": str(tmp / "out"), "data_dir": str(tmp / "data"),
            "push": {"enabled": False, "provider": "none"}})),
            port=0, config_path=cls.cfg_path)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def _post_raw(self, path, data: bytes, ctype="application/json"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data,
                                     headers={"Content-Type": ctype}, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    def test_fifty_concurrent_reads(self):
        codes = []
        lock = threading.Lock()

        def hit():
            try:
                for _ in range(10):
                    r = self._get("/" if len(codes) % 2 else "/api/settings")
                    with lock:
                        codes.append(r.status)
            except Exception as e:  # noqa: BLE001
                with lock:
                    codes.append(str(e)[:40])
        threads = [threading.Thread(target=hit) for _ in range(50)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.perf_counter() - t0
        self.assertEqual(len([c for c in codes if c == 200]), 500, codes[:5])
        self.assertLess(dt, 30.0)

    def test_malformed_bodies(self):
        # 畸形 body 应被吞成空请求（4xx），服务器不崩；/api/run 的空请求合法不算
        for path in ("/api/settings/llm", "/api/like"):
            for body in (b"not json", b"", b'{"broken', b"\xff\xfe\x00binary",
                         b'{"id": "' + b"x" * 40_000 + b'"}'):
                with self.assertRaises(urllib.error.HTTPError):
                    self._post_raw(path, body)
        # 服务器还活着
        self.assertEqual(self._get("/api/status").status, 200)

    def test_traversal_again(self):
        for evil in ("..%2f..%2fconfig.json", "digest-..%5cconfig.json",
                     "digest-%2e%2e%5cconfig.json", "digest-x.html.exe"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(f"/digest/{evil}")
            self.assertEqual(ctx.exception.code, 404)


class TestDigestEndToEndStress(unittest.TestCase):
    """mock 源 800 篇注入 + LLM 失败降级：端到端正确性与护栏耗时。"""

    def test_big_batch_degrades_gracefully(self):
        from litzaobao.config import DEFAULTS, Config, deep_merge
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(raw=deep_merge(DEFAULTS, {
                "output_dir": str(tmp / "out"), "data_dir": str(tmp / "data"),
                "push": {"enabled": False, "provider": "none"},
                "top_n": 20, "llm": {"api_key": "k", "active": "zhipu",
                                     "providers": {"zhipu": {"api_key": "k", "model": "m"}}}}))
            batch = [make_paper(i) for i in range(800)]
            t0 = time.perf_counter()
            with patch("litzaobao.digest.arxiv_source.fetch", return_value=batch), \
                 patch("litzaobao.digest.crossref_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.openalex_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.elsevier_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.enrich_mod.enrich_abstracts", return_value=0), \
                 patch("litzaobao.summarize.call_llm",
                       side_effect=RuntimeError("LLM 全挂（压测注入）")), \
                 patch("litzaobao.llm_providers.embeddings",
                       side_effect=RuntimeError("embedding 压测注入（不打真实 API）")):
                result = run_digest(cfg, no_push=True)
            dt = time.perf_counter() - t0
            self.assertEqual(result["count"], 20)
            self.assertIn("降级", (tmp / "out" / f"digest-{date.today()}.md"
                                  ).read_text(encoding="utf-8"))
            self.assertLess(dt, 15.0)
            # fd 释放：数据目录可删（Windows 句柄硬证据）
            import shutil
            shutil.rmtree(tmp / "data")


if __name__ == "__main__":
    unittest.main()
