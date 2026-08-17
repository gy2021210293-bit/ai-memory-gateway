import unittest

import main


class RequestDebugTests(unittest.TestCase):
    def setUp(self):
        self.previous = main._last_request_digest
        main._last_request_digest = None

    def tearDown(self):
        main._last_request_digest = self.previous

    def test_request_comparison_accepts_equal_append_shorten_and_empty(self):
        base = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        main.dump_request_debug("model", base)
        main.dump_request_debug("model", base)
        main.dump_request_debug("model", base + [{"role": "user", "content": "next"}])
        main.dump_request_debug("model", base[:1])
        main.dump_request_debug("model", [])

    def test_safe_debug_never_raises(self):
        original = main.dump_request_debug
        main.dump_request_debug = lambda *_args: (_ for _ in ()).throw(IndexError("boom"))
        try:
            main.safe_dump_request_debug("model", [])
        finally:
            main.dump_request_debug = original


if __name__ == "__main__":
    unittest.main()
