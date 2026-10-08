import unittest

from llm_json import parse_json_array, parse_json_object, valid_merged_ids


class ParseJsonArrayTests(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(parse_json_array('[{"id": 1}]'), [{"id": 1}])

    def test_ignores_bracketed_reasoning_before_json(self):
        raw = '[分析] 我整理好了。\n```json\n[{"id": 2}]\n```'
        self.assertEqual(parse_json_array(raw), [{"id": 2}])

    def test_prefers_final_non_empty_array_over_earlier_empty_example(self):
        raw = '不能返回空数组 []。最终结果：[{"id": 4, "merged_ids": [1, 2]}]'
        self.assertEqual(
            parse_json_array(raw),
            [{"id": 4, "merged_ids": [1, 2]}],
        )

    def test_returns_empty_only_when_no_non_empty_array_exists(self):
        self.assertEqual(parse_json_array('没有事件：[]'), [])

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
            valid_merged_ids([3, 2, 2, 999, "3", " 1 ", True], {1, 2, 3}),
            [1, 2, 3],
        )

    def test_merged_ids_reject_non_numeric_labels(self):
        self.assertEqual(
            valid_merged_ids(["ID=3", "three", 3.0], {1, 2, 3}),
            [],
        )

    def test_invalid_merged_ids_are_empty(self):
        self.assertEqual(valid_merged_ids(None, {1, 2}), [])


class ParseJsonObjectTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json_object('{"summary": "概要"}'), {"summary": "概要"})

    def test_ignores_reasoning_before_json(self):
        raw = "这段对话值得记录，但输出在最终 JSON 前被截断```json\n{\"summary\": \"最终\"}\n```"
        self.assertEqual(parse_json_object(raw), {"summary": "最终"})

    def test_prefers_final_non_empty_object_over_earlier_example(self):
        raw = '思考示例 {"summary": "旧"}。最终结果：{"summary": "新", "stable_facts": ["a"]}'
        self.assertEqual(
            parse_json_object(raw),
            {"summary": "新", "stable_facts": ["a"]},
        )

    def test_returns_empty_only_when_no_non_empty_object_exists(self):
        self.assertEqual(parse_json_object('没有概况：{}'), {})

    def test_allows_unescaped_newline_in_string(self):
        self.assertEqual(
            parse_json_object('{"summary": "第一行\n第二行"}'),
            {"summary": "第一行\n第二行"},
        )

    def test_repairs_trailing_comma(self):
        self.assertEqual(parse_json_object('{"summary": "a",}'), {"summary": "a"})

    def test_rejects_missing_object(self):
        with self.assertRaises(ValueError):
            parse_json_object('没有可用对象的内容')

    def test_finds_object_wrapped_in_array(self):
        self.assertEqual(
            parse_json_object('[{"summary": "包装在数组里"}]'),
            {"summary": "包装在数组里"},
        )


if __name__ == "__main__":
    unittest.main()
