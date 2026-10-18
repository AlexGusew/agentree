import threading
import time
import unittest

from agentree.sources import _par


class RunAllTest(unittest.TestCase):
    def test_results_aligned(self):
        def boom():
            raise ValueError("x")

        out = _par.run_all([lambda: 1, boom, lambda: 3], workers=2)
        self.assertEqual(out[0], (_par.OK, 1))
        self.assertEqual(out[1][0], _par.ERR)
        self.assertIsInstance(out[1][1], ValueError)
        self.assertEqual(out[2], (_par.OK, 3))
        self.assertEqual(_par.value(out[1], "d"), "d")
        self.assertEqual(_par.run_all([]), [])

    def test_deadline_abandons_and_skips(self):
        release = threading.Event()
        ran = []

        def slow(i):
            ran.append(i)
            release.wait(5)
            return i

        t0 = time.monotonic()
        out = _par.run_all([lambda i=i: slow(i) for i in range(4)], workers=1, deadline=time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual([o[0] for o in out], [_par.PENDING] * 4)
        release.set()
        time.sleep(0.1)
        self.assertEqual(ran, [0])          # items not started by the deadline are skipped

    def test_threads_are_daemons(self):
        release = threading.Event()
        seen = []

        def f():
            seen.append(threading.current_thread().daemon)
            release.wait(2)

        _par.run_all([f, f], deadline=time.monotonic() + 0.1)
        release.set()
        self.assertEqual(seen, [True, True])

    def test_bounded_concurrency(self):
        lock = threading.Lock()
        cur = [0, 0]

        def f():
            with lock:
                cur[0] += 1
                cur[1] = max(cur[1], cur[0])
            time.sleep(0.02)
            with lock:
                cur[0] -= 1

        _par.run_all([f] * 12, workers=3)
        self.assertLessEqual(cur[1], 3)

    def test_abandoned_work_does_not_block_interpreter_exit(self):
        import os
        import subprocess
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import time; from agentree.sources import _par; "
                "_par.run_all([lambda: time.sleep(5)] * 3, deadline=time.monotonic() + 0.1); "
                "import sys; assert 'concurrent.futures' not in sys.modules")
        t0 = time.monotonic()
        subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=10)
        self.assertLess(time.monotonic() - t0, 1.5)


if __name__ == "__main__":
    unittest.main()
