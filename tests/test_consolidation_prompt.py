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
            entity_priors="（无）",
        )

        self.assertIn("按事件主题拆分，不按日期硬塞", formatted)
        self.assertIn("content不超过200个汉字", formatted)
        self.assertIn("保留不可替代的重要原话", formatted)
        self.assertIn("我为她高兴", formatted)
        self.assertIn('"event_date":', formatted)
        self.assertIn('"merged_ids":', formatted)
        self.assertNotIn("建议上限350字", formatted)

    def test_prior_knowledge_block_and_consistency_rule(self):
        formatted = self.prompt.format(
            fragments="[ID=1] 示例",
            entities_roster="（暂无已知实体）",
            entity_priors=(
                "- Supabase（project）\n"
                "  说明：有两个账号，一个存插件数据，一个存设备数据\n"
                "  既有状态快照（最近 5 条）：2026-07-20 住在上海\n"
                "  活跃稳定特征：长期写作"
            ),
        )
        # 先验知识块渲染了说明 + 既有状态快照 + 活跃稳定特征
        self.assertIn("<已知实体先验知识（说明 + 既有状态快照 + 活跃稳定特征）>", formatted)
        self.assertIn("- Supabase（project）", formatted)
        self.assertIn("说明：有两个账号，一个存插件数据，一个存设备数据", formatted)
        self.assertIn("既有状态快照（最近 5 条）：2026-07-20 住在上海", formatted)
        self.assertIn("活跃稳定特征：长期写作", formatted)
        # 一致性约束：说明/快照/特征是结构性事实背景，不是待生成的状态，不得复述成 state_changes
        self.assertIn("结构性事实背景", formatted)
        self.assertIn("不要把它们复述成 state_changes", formatted)
        self.assertIn("不得与对应实体的说明、既有快照或活跃稳定特征矛盾", formatted)

    def test_consolidation_wiring_feeds_only_related_entity_priors(self):
        # 先验知识只从这批碎片实际关联到的实体取说明，且必须传进 format
        self.assertIn("get_entities_for_memory_ids(fragment_ids)", self.source)
        self.assertIn("entity_priors", self.source)
        self.assertIn("entity_priors=entity_priors", self.source)
        # 先验知识里还注入了既有状态快照（截取最近 N 条）与活跃稳定特征
        self.assertIn("既有状态快照", self.source)
        self.assertIn("活跃稳定特征", self.source)
        self.assertIn("ENTITY_PRIOR_SNAPSHOT_LIMIT", self.source)

    def test_fragment_input_includes_importance(self):
        self.assertIn("[重要度：{fragment.get('importance', 5)}/10]", self.source)


if __name__ == "__main__":
    unittest.main()
