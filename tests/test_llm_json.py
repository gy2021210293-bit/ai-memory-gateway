import unittest

from llm_json import parse_json_array


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


if __name__ == "__main__":
    unittest.main()
