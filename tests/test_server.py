import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest.mock import patch

from litzaobao.config import DEFAULTS, Config, deep_merge
from litzaobao.paper import Paper
from litzaobao.profile import load_likes
from litzaobao.server import _STATE, make_server

TODAY = date.today().isoformat()


def make_cfg(tmp: Path) -> Config:
    return Config(raw=deep_merge(DEFAULTS, {
        "output_dir": str(tmp / "out"),
        "data_dir": str(tmp / "data"),
        "push": {"enabled": False, "provider": "none"},
    }))


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        out = tmp / "out"
        out.mkdir(parents=True)
        (out / f"digest-{TODAY}.html").write_text("<html>digest</html>", encoding="utf-8")
        (out / f"digest-{TODAY}.json").write_text(json.dumps([
            {"id": "a:1", "source": "arxiv", "title": "LLM agent paper",
             "abstract": "abs", "url": "https://arxiv.org/abs/1", "published": TODAY,
             "doi": "", "journal": ""}]), encoding="utf-8")
        cls.server = make_server(make_cfg(tmp), port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)


class TestPages(ServerCase):
    def test_home(self):
        body = self.get("/").read().decode("utf-8")
        self.assertIn("lit-zaobao 控制台", body)
        self.assertIn(f"digest-{TODAY}", body)
        self.assertIn("LLM agent paper", body)
        self.assertIn("期刊分区", body)

    def test_serve_digest_html(self):
        body = self.get(f"/digest/digest-{TODAY}.html").read().decode()
        self.assertIn("digest", body)

    def test_traversal_blocked(self):
        for evil in ("..%2f..%2fconfig.json", "secret.txt", "digest-x.exe"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(f"/digest/{evil}")
            self.assertEqual(ctx.exception.code, 404)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)


class TestLikeApi(ServerCase):
    def test_like_unlike_roundtrip(self):
        resp = json.loads(self.post("/api/like", {"id": "a:1"}).read())
        self.assertTrue(resp["ok"])
        likes = load_likes(Path(self.tmp.name) / "data")
        self.assertEqual(len(likes), 1)
        self.assertEqual(likes[0]["id"], "a:1")

        resp = json.loads(self.post("/api/unlike", {"id": "a:1"}).read())
        self.assertTrue(resp["ok"])
        self.assertEqual(load_likes(Path(self.tmp.name) / "data"), [])

    def test_like_unknown_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/like", {"id": "ghost:9"})
        self.assertEqual(ctx.exception.code, 404)


class TestRunApi(ServerCase):
    def test_run_and_status(self):
        _STATE.update(running=False, log="")

        def fake_run(cfg, **kw):
            print("fake digest done")

        with patch("litzaobao.server.run_digest", side_effect=fake_run):
            resp = json.loads(self.post("/api/run", {}).read())
            self.assertTrue(resp["ok"])
            # 等后台线程跑完（stub 极快，轮询即可）
            for _ in range(50):
                status = json.loads(self.get("/api/status").read())
                if not status["running"]:
                    break
            else:
                self.fail("run worker 未结束")
            self.assertIn("fake digest done", status["log"])

        # 不在运行中时再发一次应 200；运行中再发应 409（此处验证非运行态 200）
        with patch("litzaobao.server.run_digest", side_effect=fake_run):
            resp = json.loads(self.post("/api/run", {}).read())
            self.assertTrue(resp["ok"])


class TestSettingsApi(unittest.TestCase):
    """设置框：读写 config.json（临时副本），密钥永不出服务端。用例间互不影响（setUp 重置基线）。"""

    SECRET = "sk-secret-123456789"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "out").mkdir()
        cls.cfg_path = tmp / "config.json"
        cls.server = make_server(make_cfg(tmp), port=0, config_path=cls.cfg_path)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.cfg_path.write_text(json.dumps({
            "llm": {"active": "zhipu", "system_prompt": "",
                    "providers": {"zhipu": {"api_key": self.SECRET, "model": "glm-4-flash"}}},
            "push": {"provider": "serverchan", "serverchan_sendkey": "SCTKEY1"},
        }), encoding="utf-8")

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def post(self, path, payload=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def read_cfg(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))

    def test_get_settings_never_leaks_key(self):
        body = self.get("/api/settings").read().decode("utf-8")
        data = json.loads(body)
        self.assertNotIn(self.SECRET, body)          # 密钥明文不出服务端
        self.assertNotIn("SCTKEY1", body)
        zhipu = next(p for p in data["providers"] if p["id"] == "zhipu")
        self.assertTrue(zhipu["has_api_key"])
        self.assertIn("…", zhipu["key_hint"])         # 只有掩码
        self.assertEqual(data["active"], "zhipu")
        self.assertTrue(data["push"]["has_key"])

    def test_save_llm_new_key_and_active(self):
        self.post("/api/settings/llm", {"provider_id": "deepseek", "set_active": True,
                                        "api_key": "sk-new", "model": "deepseek-reasoner"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["llm"]["active"], "deepseek")
        self.assertEqual(cfg["llm"]["providers"]["deepseek"]["api_key"], "sk-new")
        self.assertEqual(cfg["llm"]["providers"]["deepseek"]["model"], "deepseek-reasoner")
        # 原有 zhipu 配置不受影响
        self.assertEqual(cfg["llm"]["providers"]["zhipu"]["api_key"], self.SECRET)

    def test_save_llm_without_key_preserves(self):
        self.post("/api/settings/llm", {"provider_id": "zhipu", "model": "glm-4-air"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["llm"]["providers"]["zhipu"]["api_key"], self.SECRET)  # 保留
        self.assertEqual(cfg["llm"]["providers"]["zhipu"]["model"], "glm-4-air")

    def test_save_llm_clear_key(self):
        self.post("/api/settings/llm", {"provider_id": "zhipu", "clear_key": True})
        cfg = self.read_cfg()
        self.assertEqual(cfg["llm"]["providers"]["zhipu"]["api_key"], "")

    def test_save_system_prompt(self):
        self.post("/api/settings/llm", {"provider_id": "zhipu",
                                        "system_prompt": "自定义提示词X"})
        self.assertEqual(self.read_cfg()["llm"]["system_prompt"], "自定义提示词X")

    def test_llm_test_connection(self):
        with patch("litzaobao.server.test_connection", return_value=(True, "连接成功，模型回复: pong")) as m:
            resp = json.loads(self.post("/api/settings/test",
                                        {"provider_id": "zhipu"}).read())
        self.assertTrue(resp["ok"])
        # 草稿缺省时用已保存的值（含密钥）
        draft = m.call_args.args[0]
        self.assertEqual(draft["api_key"], self.SECRET)
        self.assertEqual(draft["base_url"], "https://open.bigmodel.cn/api/paas/v4")

    def test_llm_test_unknown_provider_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/settings/test", {"provider_id": "ghost"})
        self.assertEqual(ctx.exception.code, 404)

    def test_save_push_serverchan_and_pushplus(self):
        self.post("/api/settings/push", {"provider": "serverchan", "key": "SCTNEW"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["push"]["serverchan_sendkey"], "SCTNEW")
        self.post("/api/settings/push", {"provider": "pushplus", "key": "pp-token"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["push"]["provider"], "pushplus")
        self.assertEqual(cfg["push"]["pushplus_token"], "pp-token")

    def test_push_test_uses_saved_config(self):
        with patch("litzaobao.server.push_wechat", return_value=(True, "推送成功")) as m:
            resp = json.loads(self.post("/api/settings/push/test").read())
        self.assertTrue(resp["ok"])
        cfg_push = m.call_args.args[0]
        self.assertEqual(cfg_push.get("provider"), "serverchan")   # 基线配置里的值

    def test_sources_get_masked_and_save(self):
        body = self.get("/api/settings").read().decode("utf-8")
        self.assertIn("sources", body)
        resp = json.loads(self.post("/api/settings/sources", {
            "openalex_api_key": "oa-key-123456789", "elsevier_api_key": "els-key-123456789",
            "openalex_email": "me@example.edu"}).read())
        self.assertEqual(resp["updated"], 3)
        cfg = self.read_cfg()
        self.assertEqual(cfg["openalex_api_key"], "oa-key-123456789")
        self.assertEqual(cfg["elsevier_api_key"], "els-key-123456789")
        self.assertEqual(cfg["openalex_email"], "me@example.edu")
        # 再读设置：只见掩码不见明文
        body2 = self.get("/api/settings").read().decode("utf-8")
        self.assertNotIn("oa-key-123456789", body2)
        self.assertNotIn("els-key-123456789", body2)
        self.assertIn("…", json.loads(body2)["sources"]["openalex_key_hint"])

    def test_rank_settings_roundtrip(self):
        self.post("/api/settings/rank", {"provider": "onescholar", "key": "os-key-123456789"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["journal_rank"]["provider"], "onescholar")
        self.assertEqual(cfg["journal_rank"]["onescholar_key"], "os-key-123456789")
        body = self.get("/api/settings").read().decode("utf-8")
        self.assertNotIn("os-key-123456789", body)
        self.assertIn("…", json.loads(body)["rank"]["hint_onescholar"])

    def test_rank_provider_switch_keeps_both_keys(self):
        self.post("/api/settings/rank", {"provider": "onescholar", "key": "os-key-1"})
        self.post("/api/settings/rank", {"provider": "easyscholar", "key": "es-key-1"})
        # 只切服务商、不重填 Key：两家的 Key 都保留
        self.post("/api/settings/rank", {"provider": "onescholar"})
        cfg = self.read_cfg()
        self.assertEqual(cfg["journal_rank"]["provider"], "onescholar")
        self.assertEqual(cfg["journal_rank"]["onescholar_key"], "os-key-1")
        self.assertEqual(cfg["journal_rank"]["easyscholar_key"], "es-key-1")

    def test_rank_test_endpoint_uses_draft(self):
        with patch("litzaobao.server.journal_rank.test_connection",
                   return_value=(True, "OneScholar 连接成功 · Nature: 中科院 1区")) as m:
            resp = json.loads(self.post("/api/settings/rank/test",
                                        {"provider": "onescholar", "key": "draft-key"}).read())
        self.assertTrue(resp["ok"])
        self.assertEqual(m.call_args.kwargs["provider"], "onescholar")
        self.assertEqual(m.call_args.kwargs["key"], "draft-key")

    def test_translate_settings(self):
        body = self.get("/api/settings").read().decode("utf-8")
        data = json.loads(body)
        self.assertIn("translate", data)
        self.assertIn("llm", data["translate"]["engines"])
        resp = json.loads(self.post("/api/settings/translate", {
            "engine": "deepl", "deepl_api_key": "dk-123456789", "baidu_appid": "app1"}).read())
        self.assertTrue(resp["ok"])
        cfg = self.read_cfg()
        self.assertEqual(cfg["translate"]["engine"], "deepl")
        self.assertEqual(cfg["translate"]["deepl_api_key"], "dk-123456789")
        # 非法 engine 不生效
        self.post("/api/settings/translate", {"engine": "ghost"})
        self.assertEqual(self.read_cfg()["translate"]["engine"], "deepl")

    def test_translate_test_endpoint(self):
        with patch("litzaobao.server.translate_text",
                   return_value=("大模型智能体可用于文献调研。", "llm")):
            resp = json.loads(self.post("/api/settings/translate/test").read())
        self.assertTrue(resp["ok"])
        self.assertIn("大模型", resp["msg"])

    def test_settings_expose_effective_prompt(self):
        data = json.loads(self.get("/api/settings").read())
        self.assertIn(DEFAULTS["research_focus"], data["system_prompt_effective"])
        self.assertIn("research_focus", data)

    def test_prompt_default_endpoint_and_save_semantics(self):
        # 按新方向重生成默认
        r = json.loads(self.post("/api/settings/prompt/default",
                                 {"focus": "多智能体协作"}).read())
        self.assertIn("多智能体协作", r["text"])
        # 保存：与默认一致 → 存空串（跟随内置）；研究方向写回
        from litzaobao.summarize import default_system_prompt
        self.post("/api/settings/llm", {"provider_id": "zhipu",
                                        "research_focus": "多智能体协作",
                                        "system_prompt": default_system_prompt("多智能体协作")})
        cfg = self.read_cfg()
        self.assertEqual(cfg["research_focus"], "多智能体协作")
        self.assertEqual(cfg["llm"].get("system_prompt", ""), "")
        # 手写内容 → 存自定义
        self.post("/api/settings/llm", {"provider_id": "zhipu", "system_prompt": "我的版本"})
        self.assertEqual(self.read_cfg()["llm"]["system_prompt"], "我的版本")


class TestStatusMarker(ServerCase):
    def test_status_has_app_marker(self):
        status = json.loads(self.get("/api/status").read())
        self.assertEqual(status["app"], "lit-zaobao")
        self.assertIn("version", status)

    def test_find_running_instance(self):
        from litzaobao.server import find_running_instance
        url = find_running_instance(self.port, 1)
        self.assertEqual(url, f"http://127.0.0.1:{self.port}")
        # 未占用端口段 → None
        self.assertIsNone(find_running_instance(self.port + 100, 3))


class TestShutdown(unittest.TestCase):
    def test_shutdown_endpoint(self):
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "out").mkdir()
        server = make_server(make_cfg(Path(tmp.name)), port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch("litzaobao.server.os._exit") as mock_exit:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"})
                resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
                self.assertTrue(resp["ok"])
                for _ in range(30):
                    if mock_exit.called:
                        break
                    import time
                    time.sleep(0.1)
                mock_exit.assert_called_with(0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
