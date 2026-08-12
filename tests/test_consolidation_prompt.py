import ast
import unittest
from pathlib import Path


class ConsolidationPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.prompt = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "CONSOLIDATION_PROMPT"
                for target in node.targets
            )
        )

    def test_prompt_preserves_event_storage_contract(self):
        formatted = self.prompt.format(
            fragments="[ID=1] 示例",
            entities_roster="（暂无已知实体）",
        )

        self.assertIn("按事件主题拆分，不按日期硬塞", formatted)
        self.assertIn("content不超过200个汉字", formatted)
        self.assertIn("保留不可替代的重要原话", formatted)
        self.assertIn("我有点心疼她", formatted)
        self.assertIn('"event_date":', formatted)
        self.assertIn('"merged_ids":', formatted)
        self.assertNotIn("建议上限350字", formatted)

    def test_fragment_input_includes_importance(self):
        self.assertIn("[重要度：{fragment.get('importance', 5)}/10]", self.source)


if __name__ == "__main__":
    unittest.main()
