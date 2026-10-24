import unittest

from litzaobao.sources.openalex_source import rebuild_abstract


class TestOpenAlex(unittest.TestCase):
    def test_rebuild_abstract(self):
        inv = {"We": [0], "study": [1], "agents": [3], "reasoning": [2]}
        self.assertEqual(rebuild_abstract(inv), "We study reasoning agents")

    def test_rebuild_empty(self):
        self.assertEqual(rebuild_abstract(None), "")
        self.assertEqual(rebuild_abstract({}), "")


if __name__ == "__main__":
    unittest.main()
