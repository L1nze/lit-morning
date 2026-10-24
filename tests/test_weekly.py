import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from litzaobao.weekly import build_weekly_markdown, load_recent_digests

TODAY = date.today()


def write_digest(out: Path, day: str, records: list[dict]) -> None:
    (out / f"digest-{day}.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


class TestWeekly(unittest.TestCase):
    def test_load_filters_by_date(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            write_digest(out, TODAY.isoformat(), [{"id": "a"}])
            write_digest(out, (TODAY - timedelta(days=2)).isoformat(), [{"id": "b"}])
            write_digest(out, (TODAY - timedelta(days=30)).isoformat(), [{"id": "old"}])
            rows = load_recent_digests(out, days=7)
            ids = {r.get("id") for _, r in rows}
            self.assertEqual(ids, {"a", "b"})  # 30 天前的被过滤

    def test_markdown_content(self):
        rows = [
            (TODAY.isoformat(), {"source": "crossref",
                                 "journal": "Journal of Machine Learning Research",
                                 "journal_tier": 2, "matched": ["agent", "llm"]}),
            ((TODAY - timedelta(days=1)).isoformat(),
             {"source": "arxiv", "journal": "", "journal_tier": 3, "matched": ["reasoning"]}),
        ]
        md = build_weekly_markdown(rows, days=7)
        self.assertIn("共收录 **2** 篇", md)
        self.assertIn("Crossref 1", md)
        self.assertIn("⭐ 权威期刊 1", md)
        self.assertIn("普通 1", md)
        self.assertIn("agent", md)

    def test_empty(self):
        md = build_weekly_markdown([], days=7)
        self.assertIn("没有日报数据", md)


if __name__ == "__main__":
    unittest.main()
