"""归档页（index.html）与 RSS（feed.xml）生成测试。"""
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from litzaobao.archive import write_index, write_rss


def make_day(out: Path, day: str, n: int = 3) -> None:
    """造一天假 digest：json 论文列表 + 空 html 占位（与真实 output/ 同构）。"""
    papers = [{
        "id": f"arxiv:{day}-{i}", "source": "arxiv",
        "title": f"Paper {i} of {day} <b>raw</b>",
        "authors": ["Alice"], "abstract": "abstract text",
        "url": "https://arxiv.org/abs/x", "published": day, "doi": "",
        "journal": "", "journal_tier": 3, "journal_rank": None,
        "score": 5, "matched": ["llm"], "summary_zh": "中文摘要",
        "highlight": f"命中 paper {i} & ]]> 注入",
    } for i in range(1, n + 1)]
    (out / f"digest-{day}.json").write_text(
        json.dumps(papers, ensure_ascii=False), encoding="utf-8")
    (out / f"digest-{day}.html").write_text("<html></html>", encoding="utf-8")


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        make_day(self.out, "2026-10-22", n=3)
        make_day(self.out, "2026-10-23", n=5)
        self.addCleanup(self._tmp.cleanup)


class TestWriteIndex(ArchiveBase):
    def test_index_lists_days_and_top3(self):
        result = write_index(self.out)
        self.assertEqual(result, self.out / "index.html")
        page = result.read_text(encoding="utf-8")
        self.assertIn("lit-zaobao · 文献早报归档", page)      # 页头
        self.assertIn('href="feed.xml"', page)               # RSS 链接
        self.assertIn("2026-10-23 · 5 篇", page)             # 每天一张卡片
        self.assertIn("2026-10-22 · 3 篇", page)
        self.assertIn('href="digest-2026-10-23.html"', page)  # 相对路径链接
        self.assertLess(page.index("2026-10-23"),
                        page.index("2026-10-22"))            # 日期降序
        self.assertIn("Paper 3 of 2026-10-23", page)          # top3 的第 3 篇
        self.assertNotIn("Paper 4 of 2026-10-23", page)       # 第 4 篇不入 top3
        self.assertNotIn("<b>raw</b>", page)                  # 标题已转义

    def test_empty_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as t:
            empty = Path(t)
            self.assertIsNone(write_index(empty))
            self.assertIsNone(write_rss(empty))
            self.assertFalse((empty / "index.html").exists())  # 不写文件
            self.assertFalse((empty / "feed.xml").exists())


class TestWriteRss(ArchiveBase):
    def test_rss_parses_and_fields(self):
        result = write_rss(self.out, site_url="https://example.com/base/")
        self.assertEqual(result, self.out / "feed.xml")
        root = ET.fromstring(result.read_bytes())
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertEqual(channel.findtext("title"), "lit-zaobao 文献早报")
        self.assertEqual(channel.findtext("link"), "https://example.com/base/")  # link=site_url 原样
        items = channel.findall("item")
        self.assertEqual(len(items), 2)
        first = items[0]   # 日期降序，最新在前
        self.assertEqual(first.findtext("title"),
                         "📚 文献早报 2026-10-23 · 5 篇")
        link = "https://example.com/base/digest-2026-10-23.html"
        self.assertEqual(first.findtext("link"), link)      # site_url 拼接
        self.assertEqual(first.findtext("guid"), link)      # guid = link
        self.assertRegex(first.findtext("pubDate"),
                         r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} "
                         r"\d{4} 08:30:00 \+0800$")         # RFC 822 东八区
        desc = first.findtext("description")
        self.assertIn("Paper 5 of 2026-10-23", desc)        # top5 的第 5 篇
        self.assertIn("命中 paper 1 & ]]> 注入", desc)       # CDATA 拆开后原样读回
        self.assertIn("<b>raw</b>", desc)                   # 标题原始字符读回

    def test_rss_relative_links_without_site_url(self):
        write_rss(self.out)
        root = ET.fromstring((self.out / "feed.xml").read_bytes())
        channel = root.find("channel")
        self.assertEqual(channel.findtext("link"), "output/")  # 空时兜底
        self.assertEqual(channel.findall("item")[0].findtext("link"),
                         "digest-2026-10-23.html")             # 相对文件名


if __name__ == "__main__":
    unittest.main()
