import unittest

from llm_json import is_ai_first_person_memory, parse_json_array, valid_merged_ids


class ParseJsonArrayTests(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(parse_json_array('[{"id": 1}]'), [{"id": 1}])

    def test_ignores_bracketed_reasoning_before_json(self):
        raw = '[分析] 我整理好了。\n```json\n[{"id": 2}]\n```'
        self.assertEqual(parse_json_array(raw), [{"id": 2}])

    def test_allows_unescaped_newline_in_string(self):
        self.assertEqual(
            parse_json_array('[{"content": "第一行\n第二行"}]'),
            [{"content": "第一行\n第二行"}],
        )

    def test_repairs_trailing_comma(self):
        self.assertEqual(parse_json_array('[{"id": 3,},]'), [{"id": 3}])

    def test_rejects_missing_array(self):
        with self.assertRaises(ValueError):
            parse_json_array('{"id": 1}')

    def test_merged_ids_only_include_available_integer_fragments(self):
        self.assertEqual(
            valid_merged_ids([3, 2, 2, 999, "3", True], {1, 2, 3}),
            [2, 3],
        )

    def test_invalid_merged_ids_are_empty(self):
        self.assertEqual(valid_merged_ids(None, {1, 2}), [])

    def test_accepts_ai_first_person_narration(self):
        self.assertTrue(is_ai_first_person_memory("2026年7月19日，她告诉我准备出发，我记住了这次约定。"))

    def test_rejects_third_person_ai_narration(self):
        self.assertFalse(is_ai_first_person_memory("2026年7月19日，向野记下了她准备出发的约定。"))

    def test_user_quote_does_not_fake_ai_first_person(self):
        self.assertFalse(is_ai_first_person_memory("她说：“我准备出发”，向野记下了这次约定。"))


if __name__ == "__main__":
    unittest.main()
