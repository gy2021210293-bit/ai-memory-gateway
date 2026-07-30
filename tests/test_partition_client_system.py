import unittest
from message_pipeline import classify_request, combine_system_prompt


def build_prompt(base_prompt, messages):
    classified = classify_request(messages)
    client_chars = len("\n\n".join(classified.client_system_prompts))
    return (
        combine_system_prompt(base_prompt, classified.client_system_prompts),
        len(classified.client_system_prompts),
        client_chars,
    )


class PartitionClientSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_prompt = staticmethod(build_prompt)

    def test_appends_all_client_system_prompts_after_gateway_prompt(self):
        messages = [
            {"role": "system", "content": "workflow role"},
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "system", "content": "output rules"},
            {"role": "user", "content": "current request"},
        ]

        prompt, count, chars = self.build_prompt("gateway rules", messages)

        self.assertEqual(
            prompt,
            "gateway rules\n\nworkflow role\n\noutput rules",
        )
        self.assertEqual(count, 2)
        self.assertEqual(chars, len("workflow role\n\noutput rules"))

    def test_preserves_text_blocks_in_order(self):
        messages = [{
            "role": "system",
            "content": [
                {"type": "text", "text": "first block"},
                {"type": "image_url", "image_url": {"url": "ignored"}},
                {"type": "text", "text": "second block"},
            ],
        }]

        prompt, count, chars = self.build_prompt("", messages)

        self.assertEqual(prompt, "first block\nsecond block")
        self.assertEqual(count, 1)
        self.assertEqual(chars, len(prompt))

    def test_no_client_system_keeps_gateway_prompt_unchanged(self):
        prompt, count, chars = self.build_prompt(
            "gateway rules",
            [{"role": "user", "content": "request"}],
        )

        self.assertEqual(prompt, "gateway rules")
        self.assertEqual(count, 0)
        self.assertEqual(chars, 0)


if __name__ == "__main__":
    unittest.main()
