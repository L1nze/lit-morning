import threading
import unittest

from litzaobao.gui import UiQueue


class TestUiQueue(unittest.TestCase):
    def test_order_and_drain(self):
        q = UiQueue()
        q.put(lambda: None)
        results = []

        def job(n):
            results.append(n)
        for i in range(5):
            q.put(lambda i=i: job(i))
        drained = q.drain()
        self.assertEqual(len(drained), 6)
        for fn in drained:
            fn()
        self.assertEqual(results, [0, 1, 2, 3, 4])

    def test_empty_drain(self):
        self.assertEqual(UiQueue().drain(), [])

    def test_thread_safe_put(self):
        q = UiQueue()

        def worker():
            for _ in range(100):
                q.put(lambda: None)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(q.drain()), 400)


if __name__ == "__main__":
    unittest.main()
