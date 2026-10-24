"""期刊分区：字段归一、两家服务商请求、缓存、管线渲染。"""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from litzaobao import journal_rank
from litzaobao.config import DEFAULTS, Config, apply_env, deep_merge
from litzaobao.digest import run_digest
from litzaobao.paper import Paper
from litzaobao.render import _meta_line

EASY_DATA = {"officialRank": {"all": {
    "sci": "Q1", "sciBase": "1区", "sciUp": "1区", "sciUpTop": "是",
    "sciif": "48.5", "jci": "11.14", "pku": "北大核心"}}}
EASY_RESPONSE = {"code": 200, "msg": "SUCCESS", "data": EASY_DATA}


def raw_cfg(**over) -> dict:
    return {"journal_rank": {"provider": "easyscholar", "easyscholar_key": "k",
                             "onescholar_key": "o", "cache_days": 30, **over}}


class TestNormalize(unittest.TestCase):
    def test_easyscholar_official_rank_all(self):
        rank = journal_rank.normalize(EASY_DATA, "easyscholar")
        self.assertEqual(rank["cas"], "1区")
        self.assertTrue(rank["cas_top"])
        self.assertEqual(rank["jcr"], "Q1")
        self.assertAlmostEqual(rank["imf"], 48.5)
        self.assertAlmostEqual(rank["jci"], 11.14)
        self.assertTrue(rank["beihe"])
        self.assertEqual(rank["provider"], "easyscholar")

    def test_select_overrides_all(self):
        data = {"officialRank": {
            "all": {"sci": "Q2", "sciBase": "2区"},
            "select": {"sci": "Q1", "sciBase": "1区", "sciUpTop": "Top"},
        }}
        rank = journal_rank.normalize(data, "easyscholar")
        self.assertEqual(rank["jcr"], "Q1")
        self.assertEqual(rank["cas"], "1区")
        self.assertTrue(rank["cas_top"])

    def test_flat_payload_and_cas_prefix(self):
        rank = journal_rank.normalize(
            {"jcr": "1", "cas": "中科院 2 区", "imf": 12.3, "xr": "1区", "xr_top": True}, "onescholar")
        self.assertEqual(rank["jcr"], "Q1")
        self.assertEqual(rank["cas"], "2区")
        self.assertAlmostEqual(rank["imf"], 12.3)
        self.assertEqual(rank["xr"], "1区")
        self.assertTrue(rank["xr_top"])

    def test_no_data_returns_none(self):
        self.assertIsNone(journal_rank.normalize({"officialRank": {"all": {}}}, "easyscholar"))
        self.assertIsNone(journal_rank.normalize({}, "easyscholar"))
        self.assertIsNone(journal_rank.normalize(None, "easyscholar"))

    def test_onescholar_public_api_fields(self):
        # 与 https://open.scigreat.com/api/v1/getrank 文档示例一致
        data = {"abbr": "Nature", "cas": "1区", "cas_top": "中科院 Top",
                "jcr": "Q1", "imf": 48.5, "jci": 11.14, "if5": 55,
                "xr": "1区", "xr_top": "新锐 Top"}
        rank = journal_rank.normalize(data, "onescholar")
        self.assertEqual(rank["cas"], "1区")
        self.assertTrue(rank["cas_top"])
        self.assertEqual(rank["jcr"], "Q1")
        self.assertAlmostEqual(rank["imf"], 48.5)
        self.assertEqual(rank["xr"], "1区")
        self.assertTrue(rank["xr_top"])
        text = journal_rank.summary(rank)
        self.assertIn("中科院 1区", text)
        self.assertIn("Top", text)
        self.assertIn("JCR Q1", text)

    def test_chips_and_summary(self):
        rank = journal_rank.normalize(EASY_DATA, "easyscholar")
        text = journal_rank.summary(rank)
        self.assertIn("中科院 1区", text)
        self.assertIn("Top", text)
        self.assertIn("JCR Q1", text)
        self.assertIn("IF 48.5", text)
        self.assertEqual(journal_rank.summary(None), "")
        self.assertEqual(journal_rank.chips(None), [])


class TestQuery(unittest.TestCase):
    def test_easyscholar_url_and_payload(self):
        with patch("litzaobao.journal_rank.http_get", return_value=json.dumps(EASY_RESPONSE)) as get:
            data = journal_rank.query("easyscholar", "secret", "Nature Reviews & Molecular Biology")
        self.assertEqual(data["officialRank"]["all"]["sci"], "Q1")
        args, kwargs = get.call_args
        self.assertEqual(args[0], journal_rank.EASYSCHOLAR_URL)
        self.assertEqual(kwargs["params"]["secretKey"], "secret")
        self.assertEqual(kwargs["params"]["publicationName"], "Nature Reviews & Molecular Biology")

    def test_easyscholar_error_raises_readable(self):
        with patch("litzaobao.journal_rank.http_get",
                   return_value=json.dumps({"code": 40002, "msg": "Key错误", "data": None})):
            with self.assertRaises(journal_rank.RankError) as ctx:
                journal_rank.query("easyscholar", "bad", "Nature")
        self.assertIn("Key错误", str(ctx.exception))

    def test_onescholar_post_and_result(self):
        payload = {"code": 200, "status": "success", "results": [
            {"query": {"journal": ["Nature"]},
             "data": {"jcr": "Q1", "cas": "1区", "imf": 48.5}}]}
        with patch("litzaobao.journal_rank.http_post", return_value=json.dumps(payload)) as post:
            data = journal_rank.query("onescholar", "token", "Nature")
        self.assertEqual(data["cas"], "1区")
        args, kwargs = post.call_args
        self.assertEqual(args[0], journal_rank.ONESCHOLAR_URL)
        self.assertEqual(args[1], [{"journal": ["Nature"]}])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")

    def test_onescholar_falls_back_to_legacy_on_404(self):
        import urllib.error
        legacy = {"code": 200, "results": [{"data": {"cas": "1区"}}]}
        missing = urllib.error.HTTPError(journal_rank.ONESCHOLAR_URL, 404, "Not Found", {}, None)
        with patch("litzaobao.journal_rank.http_post",
                   side_effect=[missing, json.dumps(legacy)]) as post:
            data = journal_rank.query("onescholar", "k", "Nature")
        self.assertEqual(data["cas"], "1区")
        self.assertEqual(post.call_args_list[1].args[0], journal_rank.ONESCHOLAR_LEGACY_URL)

    def test_onescholar_falls_back_to_web_on_double_404(self):
        import urllib.error
        missing = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        web_html = ('<thead><tr><th>期刊名称</th></tr></thead><tbody>'
                    '<tr><td class="journal-name"><a href="/i/rank/?s=Nature">'
                    '<span class="search-highlight">Nature</span></a></td></td>'
                    '<td>0028-0836</td>'
                    '<td><span>56.1</span><span class="journal-imf-up">↑ 7.6</span></td>'
                    '<td>Q1</td><td>综合性期刊1区 Top</td><td>综合性期刊1区 Top</td>'
                    '<td>-</td><td>-</td></tr></tbody>')
        web = json.dumps({"code": 200, "data": web_html})
        with patch("litzaobao.journal_rank.http_post",
                   side_effect=[missing, missing]) as post,              patch("litzaobao.journal_rank.http_get", return_value=web) as get:
            data = journal_rank.query("onescholar", "k", "Nature")
        self.assertEqual(post.call_count, 2)   # 新旧接口都试过
        self.assertEqual(get.call_args.kwargs["params"]["kw"], "Nature")
        self.assertEqual(data["sci"], "Q1")
        self.assertEqual(data["sciif"], "56.1")
        self.assertEqual(data["sciBase"], "综合性期刊1区")
        self.assertTrue(data["sciUpTop"])
        rank = journal_rank.normalize(data, "onescholar")
        self.assertEqual(rank["cas"], "综合性期刊1区")
        self.assertTrue(rank["cas_top"])
        self.assertEqual(rank["jcr"], "Q1")
        self.assertEqual(rank["imf"], 56.1)

    def test_onescholar_web_no_rows_returns_empty(self):
        import urllib.error
        missing = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        web = json.dumps({"code": 201, "data": "<tbody><tr><td>总计查询到 0 条</td></tr></tbody>"})
        with patch("litzaobao.journal_rank.http_post", side_effect=[missing, missing]),              patch("litzaobao.journal_rank.http_get", return_value=web):
            data = journal_rank.query("onescholar", "k", "NoSuchJournal")
        self.assertEqual(data, {})
        self.assertIsNone(journal_rank.normalize(data, "onescholar"))

    def test_onescholar_api_key_error_message(self):
        payload = {"code": 403, "status": "invalid_api_key_provided",
                   "message": "API Key invalid"}
        with patch("litzaobao.journal_rank.http_post", return_value=json.dumps(payload)):
            with self.assertRaises(journal_rank.RankError) as ctx:
                journal_rank.query("onescholar", "bad", "Nature")
        self.assertIn("重新生成", str(ctx.exception))

    def test_onescholar_http_error_translated(self):
        import urllib.error
        err = urllib.error.HTTPError(journal_rank.ONESCHOLAR_URL, 403, "Forbidden", {}, None)
        with patch("litzaobao.journal_rank.http_post", side_effect=err):
            with self.assertRaises(journal_rank.RankError) as ctx:
                journal_rank.query("onescholar", "bad", "Nature")
        self.assertIn("Key 无效或无权限", str(ctx.exception))


class TestCache(unittest.TestCase):
    def test_second_lookup_is_cached(self):
        with tempfile.TemporaryDirectory() as td:
            svc = journal_rank.JournalRankService(raw_cfg(), Path(td))
            with patch("litzaobao.journal_rank.query", return_value=EASY_DATA) as q:
                first = svc.lookup("Nature")
                second = svc.lookup("nature ")
            self.assertEqual(q.call_count, 1)
            self.assertEqual(first["cas"], "1区")
            self.assertEqual(second, first)
            svc.save()
            # 新实例读磁盘缓存，不再查网络
            svc2 = journal_rank.JournalRankService(raw_cfg(), Path(td))
            with patch("litzaobao.journal_rank.query") as q2:
                self.assertEqual(svc2.lookup("Nature")["imf"], 48.5)
            q2.assert_not_called()

    def test_zero_ttl_refetches(self):
        with tempfile.TemporaryDirectory() as td:
            svc = journal_rank.JournalRankService(raw_cfg(cache_days=0), Path(td))
            with patch("litzaobao.journal_rank.query", return_value=EASY_DATA) as q:
                svc.lookup("Nature")
                svc._memo.clear()
                svc.lookup("Nature")
            self.assertEqual(q.call_count, 2)

    def test_empty_result_cached_as_miss(self):
        with tempfile.TemporaryDirectory() as td:
            svc = journal_rank.JournalRankService(raw_cfg(), Path(td))
            with patch("litzaobao.journal_rank.query",
                       return_value={"officialRank": {"all": {}}}) as q:
                self.assertIsNone(svc.lookup("Unknown Journal"))
            svc.save()
            svc2 = journal_rank.JournalRankService(raw_cfg(), Path(td))
            with patch("litzaobao.journal_rank.query") as q2:
                self.assertIsNone(svc2.lookup("Unknown Journal"))
            q2.assert_not_called()
            self.assertEqual(q.call_count, 1)

    def test_failure_keeps_stale_rank(self):
        with tempfile.TemporaryDirectory() as td:
            svc = journal_rank.JournalRankService(raw_cfg(), Path(td))
            with patch("litzaobao.journal_rank.query", return_value=EASY_DATA):
                svc.lookup("Nature")
            svc.save()
            # cache_days=0 强制重查，网络失败 → 仍返回旧数据
            svc2 = journal_rank.JournalRankService(raw_cfg(cache_days=0), Path(td))
            with patch("litzaobao.journal_rank.query",
                       side_effect=journal_rank.RankError("HTTP 503")):
                rank = svc2.lookup("Nature")
            self.assertEqual(rank["cas"], "1区")
            self.assertIn("HTTP 503", svc2.error_for("nature"))
            self.assertEqual(svc2.requests, 1)   # 失败只算一次请求


class TestAnnotate(unittest.TestCase):
    def test_unique_journals_and_arxiv_skipped(self):
        papers = [
            Paper(id="doi:1", source="crossref", title="A", journal="Nature"),
            Paper(id="doi:2", source="crossref", title="B", journal="Nature"),
            Paper(id="arxiv:1", source="arxiv", title="C", journal=""),
        ]
        with tempfile.TemporaryDirectory() as td, \
             patch("litzaobao.journal_rank.query", return_value=EASY_DATA) as q:
            stats = journal_rank.annotate_papers(papers, raw_cfg(), Path(td))
        self.assertEqual(q.call_count, 1)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["hit"], 2)
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(papers[0].journal_rank["cas"], "1区")
        self.assertIsNone(papers[2].journal_rank)

    def test_disabled_without_key(self):
        papers = [Paper(id="doi:1", source="crossref", title="A", journal="Nature")]
        stats = journal_rank.annotate_papers(
            papers, {"journal_rank": {"provider": "easyscholar", "easyscholar_key": ""}},
            Path(tempfile.gettempdir()))
        self.assertFalse(stats["enabled"])
        self.assertIsNone(papers[0].journal_rank)


class TestSettings(unittest.TestCase):
    def test_apply_save_keeps_other_provider_key(self):
        cfg: dict = {}
        journal_rank.apply_rank_save(cfg, "easyscholar", "easy-key")
        journal_rank.apply_rank_save(cfg, "scigreat", "one-key")   # LitBoard 旧写法
        self.assertEqual(cfg["journal_rank"]["provider"], "onescholar")
        self.assertEqual(cfg["journal_rank"]["easyscholar_key"], "easy-key")
        self.assertEqual(cfg["journal_rank"]["onescholar_key"], "one-key")
        journal_rank.apply_rank_save(cfg, "onescholar", "")        # 空 Key 保留
        self.assertEqual(cfg["journal_rank"]["onescholar_key"], "one-key")

    def test_off_keeps_keys(self):
        cfg = {"journal_rank": {"provider": "easyscholar", "easyscholar_key": "k"}}
        journal_rank.apply_rank_save(cfg, "off")
        self.assertEqual(cfg["journal_rank"]["provider"], "off")
        self.assertEqual(cfg["journal_rank"]["easyscholar_key"], "k")

    def test_env_override(self):
        raw = deep_merge(DEFAULTS, {})
        with patch.dict(os.environ, {"LITZB_EASYSCHOLAR_KEY": "env-key",
                                     "LITZB_RANK_PROVIDER": "easyscholar"}):
            apply_env(raw)
        self.assertEqual(raw["journal_rank"]["easyscholar_key"], "env-key")


class TestRender(unittest.TestCase):
    def test_meta_line_rank(self):
        p = Paper(id="doi:1", source="crossref", title="A", journal="Nature",
                  journal_rank={"cas": "1区", "jcr": "Q1", "imf": 48.5})
        self.assertIn("中科院 1区", _meta_line(p))
        self.assertNotIn("中科院 1区", _meta_line(p, include_rank=False))

    def test_digest_writes_rank_to_md_html_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(raw=deep_merge(DEFAULTS, {
                "output_dir": str(tmp / "out"), "data_dir": str(tmp / "data"),
                "push": {"enabled": False, "provider": "none"}, "llm": {"api_key": ""},
                "journal_rank": {"provider": "easyscholar", "easyscholar_key": "k"},
            }))
            papers = [Paper(id="doi:10.1/x", source="crossref",
                            title="Multi-agent tool use for language models",
                            abstract="llm agent tool use",
                            url="https://doi.org/10.1/x", published="2026-10-16",
                            doi="10.1/x",
                            journal="Journal of Machine Learning Research")]
            with patch("litzaobao.digest.arxiv_source.fetch", return_value=papers), \
                 patch("litzaobao.digest.crossref_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.openalex_source.fetch", return_value=[]), \
                 patch("litzaobao.digest.enrich_mod.enrich_abstracts", return_value=0), \
                 patch("litzaobao.journal_rank.query", return_value=EASY_DATA):
                result = run_digest(cfg, no_push=True)
            self.assertEqual(result["count"], 1)
            md = Path(result["html"]).with_suffix(".md").read_text(encoding="utf-8")
            html = Path(result["html"]).read_text(encoding="utf-8")
            data = json.loads((tmp / "out" / f"digest-{date.today()}.json")
                              .read_text(encoding="utf-8"))
            self.assertIn("中科院 1区", md)
            self.assertIn("JCR Q1", md)
            self.assertIn("中科院 1区", html)
            self.assertEqual(data[0]["journal_rank"]["cas"], "1区")


if __name__ == "__main__":
    unittest.main()
