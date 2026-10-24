"""语义召回增强测试：全部 mock llm_providers.embeddings，不打真实 API。"""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from litzaobao import llm_providers as lp
from litzaobao import semantic
from litzaobao.paper import Paper

EPS = 1e-9   # 构造恰好越过/不到阈值的向量（余弦计算自身的浮点误差 ~1e-16，远小于它）

# 模型 + API Key 齐备的可启用配置（augment 要求两者都在才真正发起 embedding 请求）
LLM_OK = {"embedding_model": "m", "providers": {"zhipu": {"api_key": "k"}}}


def unit(t: float, sign: int = 0) -> list[float]:
    """构造与 [1,0] 夹角余弦 ≈ t 的单位向量；sign=+1 恰好过阈值 / -1 恰好不到。"""
    x = t + sign * EPS
    return [x, math.sqrt(max(0.0, 1.0 - x * x))]


def mk(pid: str, title: str, score: int = 10, matched=("llm",)) -> Paper:
    p = Paper(id=pid, source="arxiv", title=title, abstract=f"abs-{pid}")
    p.score = score
    p.matched = list(matched)
    return p


def text_of(p: Paper) -> str:
    return f"{p.title}\n{p.abstract}"


class SemanticCase(unittest.TestCase):
    """公共脚手架：临时 data_dir + 一条点赞 + 向量可控的 augment 包装。"""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.data_dir = Path(td.name)
        self.focus = "研究焦点"

    def add_like(self, title="点赞论文", abstract="赞"):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "likes.json").write_text(
            json.dumps([{"id": "a:like", "title": title, "abstract": abstract}],
                       ensure_ascii=False), encoding="utf-8")

    def augment(self, papers, rejected, vec_map, *, focus=None, llm_raw=None,
                anchor_vec=(1.0, 0.0), focus_vec=(1.0, 0.0)):
        """锚点（点赞+focus）向量可指定；候选/borderline 文本查 vec_map。"""
        self.add_like()
        focus = self.focus if focus is None else focus
        full = {"点赞论文\n赞": list(anchor_vec), focus: list(focus_vec), **vec_map}
        with patch("litzaobao.llm_providers.embeddings",
                   side_effect=lambda raw, texts, timeout=60: [full[t] for t in texts]) as m:
            out, status = semantic.augment(
                papers, rejected, focus,
                llm_raw if llm_raw is not None else
                {"embedding_model": "test-embedding", "providers": {"zhipu": {"api_key": "k"}}},
                self.data_dir)
        return out, status, m


class TestBoostTiers(SemanticCase):
    def test_tier_boundaries_and_cap(self):
        # (阈值, 越过方向, 期望加分)：≥0.75→+4、≥0.65→+3、≥0.55→+2，含边界
        cases = [(0.75, +1, 4), (0.75, -1, 3),
                 (0.65, +1, 3), (0.65, -1, 2),
                 (0.55, +1, 2), (0.55, -1, 0)]
        papers, vec_map = [], {}
        for i, (t, sign, _) in enumerate(cases):
            p = mk(f"a:{i}", f"title-{i}")
            papers.append(p)
            vec_map[text_of(p)] = unit(t, sign)
        cap = mk("a:cap", "title-cap")            # sim=1.0 → 封顶 +4
        papers.append(cap)
        vec_map[text_of(cap)] = [1.0, 0.0]

        out, status, _ = self.augment(papers, [], vec_map)

        self.assertEqual([p.id for p in out], [p.id for p in papers])
        for (t, sign, want), p in zip(cases, papers):
            self.assertEqual(p.score, 10 + want, f"sim≈{t}{sign:+d} 档位错误")
        self.assertEqual(cap.score, 14)
        self.assertIn("画像锚点 2 条", status)      # 点赞 1 + focus 1
        self.assertIn("加分 6 篇", status)

    def test_centroid_weights_focus_double(self):
        # 点赞 [1,0](权重1) + focus [0,1](权重2) → 质心 [1/3, 2/3]
        hi = mk("a:hi", "title-hi")                # [0,1] → sim=2/√5≈0.894 → +4
        lo = mk("a:lo", "title-lo")                # [1,0] → sim=1/√5≈0.447 → +0
        _, status, _ = self.augment([hi, lo], [],
                                    {text_of(hi): [0.0, 1.0], text_of(lo): [1.0, 0.0]},
                                    anchor_vec=(1.0, 0.0), focus_vec=(0.0, 1.0))
        self.assertEqual(hi.score, 14)
        self.assertEqual(lo.score, 10)
        self.assertIn("加分 1 篇", status)


class TestRescue(SemanticCase):
    def test_rescue_order_top_and_threshold(self):
        keep = mk("a:keep", "title-keep")          # 候选本体：sim=1.0 → +4
        r90 = mk("r:90", "title-r90", score=0)     # sim 0.90 → 捞回第 1
        r80 = mk("r:80", "title-r80", score=0)     # sim 0.80 → 捞回第 2
        r70 = mk("r:70", "title-r70", score=0)     # sim 0.70 够格但超出上限(默认2)
        r50 = mk("r:50", "title-r50", score=0)     # sim 0.50 < 0.60 → 不捞
        nomatch = mk("r:nomatch", "title-nm", score=0, matched=())  # matched 空 → 不算 borderline
        dup = mk("r:dup", "title-keep", score=0)   # 与候选同标题 → 去重跳过
        vec_map = {text_of(keep): [1.0, 0.0], text_of(r90): unit(0.90),
                   text_of(r80): unit(0.80), text_of(r70): unit(0.70),
                   text_of(r50): unit(0.50)}

        out, status, _ = self.augment([keep], [r90, r80, r70, r50, nomatch, dup], vec_map)

        self.assertEqual([p.id for p in out], ["a:keep", "r:90", "r:80"])
        self.assertEqual((r90.score, r80.score), (5, 5))   # 捞回基础分
        self.assertEqual(r70.score, 0)                     # 没捞回的不动
        self.assertIn("加分 1 篇", status)
        self.assertIn("捞回 2 篇", status)

    def test_rescue_top_override(self):
        r90 = mk("r:90", "t90", score=0)
        r80 = mk("r:80", "t80", score=0)
        out, status, _ = self.augment([], [r90, r80],
                                      {text_of(r90): unit(0.90), text_of(r80): unit(0.80)},
                                      llm_raw=dict(LLM_OK, semantic_rescue_top=1))
        self.assertEqual([p.id for p in out], ["r:90"])
        self.assertIn("捞回 1 篇", status)


class TestCache(SemanticCase):
    def test_second_run_hits_cache_and_only_fetches_misses(self):
        p1, p2 = mk("a:1", "t1"), mk("a:2", "t2")
        vec_map = {"点赞论文\n赞": [1.0, 0.0], self.focus: [1.0, 0.0],
                   text_of(p1): [1.0, 0.0], text_of(p2): unit(0.8)}
        self.add_like()
        with patch("litzaobao.llm_providers.embeddings",
                   side_effect=lambda raw, texts, timeout=60: [vec_map[t] for t in texts]) as m:
            _, s1 = semantic.augment([p1, p2], [], self.focus,
                                     dict(LLM_OK), self.data_dir)
            self.assertNotIn("跳过", s1)
            self.assertEqual(m.call_count, 1)     # 首轮：锚点+2 篇合成一批
            semantic.augment([p1, p2], [], self.focus, dict(LLM_OK), self.data_dir)
            self.assertEqual(m.call_count, 1)     # 二轮全命中缓存，零新请求
            p3 = mk("a:3", "t3")
            vec_map[text_of(p3)] = unit(0.6)
            semantic.augment([p1, p2, p3], [], self.focus,
                             dict(LLM_OK), self.data_dir)
            self.assertEqual(m.call_count, 2)     # 只补缺失的新文本
            self.assertEqual(m.call_args.args[1], [text_of(p3)])

        cache = json.loads((self.data_dir / "embed_cache.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cache), 4)
        self.assertTrue(all(len(k) == 16 for k in cache), "key 应为 sha256 前 16 位")
        self.assertTrue(all(isinstance(v, list) for v in cache.values()))


class TestDisabledAndDegrade(SemanticCase):
    def test_no_model_returns_unchanged(self):
        # deepseek 无 embedding 预设且未显式配置 → 未启用
        papers = [mk("a:1", "t")]
        with patch("litzaobao.llm_providers.embeddings") as m:
            out, status = semantic.augment(
                papers, [], self.focus, {"active": "deepseek"}, self.data_dir)
        self.assertIs(out, papers)
        self.assertIn("未启用", status)
        self.assertEqual(m.call_count, 0)

    def test_no_anchors_returns_unchanged(self):
        # 模型可用但 focus 与点赞均为空 → 没有画像锚点，未启用
        papers = [mk("a:1", "t")]
        with patch("litzaobao.llm_providers.embeddings") as m:
            out, status = semantic.augment(
                papers, [], "", dict(LLM_OK), self.data_dir)
        self.assertIs(out, papers)
        self.assertIn("未启用", status)
        self.assertEqual(m.call_count, 0)

    def test_no_api_key_disables(self):
        # 模型有（预设）但当前服务商没配 key → 未启用且不发请求（避免每轮白发 401）
        papers = [mk("a:1", "t")]
        with patch("litzaobao.llm_providers.embeddings") as m:
            out, status = semantic.augment(
                papers, [], self.focus, {"active": "zhipu"}, self.data_dir)
        self.assertIs(out, papers)
        self.assertIn("API Key", status)
        self.assertEqual(m.call_count, 0)

    def test_embedding_error_never_raises(self):
        self.add_like()
        papers = [mk("a:1", "t")]
        with patch("litzaobao.llm_providers.embeddings",
                   side_effect=RuntimeError("HTTP 402: 余额不足")):
            out, status = semantic.augment(
                papers, [], self.focus, dict(LLM_OK), self.data_dir)
        self.assertIs(out, papers)
        self.assertEqual(papers[0].score, 10)      # 分数原样
        self.assertIn("跳过", status)
        self.assertIn("余额不足", status)


class TestCosine(unittest.TestCase):
    def test_hand_written_cosine(self):
        self.assertAlmostEqual(semantic.cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(semantic.cosine([3, 4], [3, 4]), 1.0)
        self.assertAlmostEqual(semantic.cosine([1, 0], [2, 0]), 1.0)   # 与长度无关
        self.assertEqual(semantic.cosine([0, 0], [1, 2]), 0.0)         # 零向量
        self.assertEqual(semantic.cosine([1, 0], [1, 0, 0]), 0.0)      # 维度不符


class TestEmbeddingsClient(unittest.TestCase):
    """llm_providers.embeddings 本体：patch http_post，不打网络。"""

    def test_explicit_model_and_index_alignment(self):
        def fake_post(url, payload, **kw):
            self.assertTrue(url.endswith("/embeddings"))
            self.assertEqual(payload["model"], "my-embed")
            self.assertEqual(payload["input"], ["a", "b"])
            return json.dumps({"data": [                   # 故意乱序，验证按 index 对齐
                {"index": 1, "embedding": [0.2, 0.0]},
                {"index": 0, "embedding": [0.1, 0.0]},
            ]})

        with patch("litzaobao.llm_providers.http_post", side_effect=fake_post):
            vecs = lp.embeddings({"embedding_model": "my-embed",
                                  "providers": {"zhipu": {"api_key": "k"}}}, ["a", "b"])
        self.assertEqual(vecs, [[0.1, 0.0], [0.2, 0.0]])

    def test_provider_preset_used_when_not_explicit(self):
        captured = {}

        def fake_post(url, payload, **kw):
            captured["model"] = payload["model"]
            return json.dumps({"data": [{"index": 0, "embedding": [1.0]}]})

        with patch("litzaobao.llm_providers.http_post", side_effect=fake_post):
            lp.embeddings({"active": "zhipu"}, ["x"])
        self.assertEqual(captured["model"], "embedding-3")

    def test_no_model_raises(self):
        with self.assertRaises(RuntimeError):
            lp.embeddings({"active": "deepseek"}, ["x"])

    def test_count_mismatch_raises(self):
        with patch("litzaobao.llm_providers.http_post",
                   side_effect=lambda *a, **k: json.dumps({"data": []})):
            with self.assertRaises(RuntimeError):
                lp.embeddings({"embedding_model": "m"}, ["x", "y"])

    def test_empty_texts_no_request(self):
        with patch("litzaobao.llm_providers.http_post") as m:
            self.assertEqual(lp.embeddings({"embedding_model": "m"}, []), [])
        self.assertEqual(m.call_count, 0)


if __name__ == "__main__":
    unittest.main()
