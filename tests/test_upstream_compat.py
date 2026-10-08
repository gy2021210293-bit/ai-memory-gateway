import unittest

from upstream_compat import normalize_chat_request


class UpstreamCompatibilityTests(unittest.TestCase):
    def test_official_moonshot_kimi_drops_temperature(self):
        body = {"model": "kimi-k2.5", "temperature": 0.7, "messages": []}
        removed = normalize_chat_request(body, "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(removed, 0.7)
        self.assertNotIn("temperature", body)

    def test_moonshot_v1_keeps_temperature(self):
        body = {"model": "moonshot-v1-32k", "temperature": 0.3}
        normalize_chat_request(body, "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(body["temperature"], 0.3)

    def test_other_providers_keep_temperature(self):
        body = {"model": "kimi-k2.5", "temperature": 0.6}
        normalize_chat_request(body, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(body["temperature"], 0.6)


if __name__ == "__main__":
    unittest.main()
