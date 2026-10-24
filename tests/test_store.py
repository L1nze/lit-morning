import tempfile
import unittest
from pathlib import Path

from litzaobao.paper import Paper
from litzaobao.store import SeenStore


def p(pid, title):
    return Paper(id=pid, source="arxiv", title=title)


class TestStore(unittest.TestCase):
    def test_mark_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            with SeenStore(Path(td) / "seen.db") as store:
                a, b = p("arxiv:1", "Paper One"), p("arxiv:2", "Paper Two")
                fresh = store.filter_new([a, b])
                self.assertEqual(len(fresh), 2)
                store.mark_seen([a])
                fresh2 = store.filter_new([a, b])
                self.assertEqual([x.id for x in fresh2], ["arxiv:2"])
                # 重复 mark 不炸（INSERT OR IGNORE）
                store.mark_seen([a])

    def test_cross_source_dedup_by_title(self):
        with tempfile.TemporaryDirectory() as td:
            with SeenStore(Path(td) / "seen.db") as store:
                a = p("arxiv:1", "Same Title Here")
                store.mark_seen([a])
                cross = p("doi:10.1/x", "same  title here")
                fresh = store.filter_new([cross])
                self.assertEqual(fresh, [])  # dedup_key 相同 → 已读


if __name__ == "__main__":
    unittest.main()
