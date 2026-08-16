import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardCognitiveModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

    def test_dashboard_uses_one_integrated_review_action(self):
        self.assertIn("三元一场认知模型", self.html)
        self.assertEqual(self.html.count("onclick=\"generateCognitiveDraft(false)\""), 1)
        self.assertEqual(self.html.count("onclick=\"generateCognitiveDraft(true)\""), 1)  # 深度体检按钮
        self.assertEqual(self.html.count("onclick=\"integrateCognitiveScan()\""), 1)       # 整合扫描按钮
        self.assertNotIn("generateCognitiveDraft('user')", self.html)
        self.assertIn("id=\"cognition-review-after\"", self.html)

    def test_dashboard_renders_three_fixed_sections_and_diff(self):
        for cognitive_type in (
            "user_core", "self_core", "relationship_core"
        ):
            self.assertIn(f"cognitive_type: '{cognitive_type}'", self.js)
        self.assertNotIn("current_field", self.js)
        self.assertNotIn("'context'", self.js)
        self.assertIn("cognition-diff-grid", self.js)
        self.assertIn("可能过时", self.js)
        self.assertNotIn("user_traits_preferences", self.js)

    def test_dashboard_supports_stability_toggle_conflict_and_due_badge(self):
        # 稳定/当前开关 + 默认复核日期
        self.assertIn('id="cognition-stability"', self.html)
        self.assertIn("onCognitionStabilityChange", self.js)
        self.assertIn("defaultReviewAfter", self.js)
        # 到期提醒徽章
        self.assertIn("cognition-due-badge", self.html)
        self.assertIn("待复核", self.js)
        # 冲突裁决四按钮
        self.assertIn("resolveConflictCognitiveDraft", self.js)
        self.assertIn("保留旧卡", self.js)
        self.assertIn("用新证据取代", self.js)

    def test_cognition_layout_is_responsive_without_fixed_min_width(self):
        self.assertIn(".cognition-grid", self.css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", self.css)
        self.assertIn("min-width: 0", self.css)

    def test_dashboard_supports_atomic_cards_with_levels_and_lifecycle(self):
        # 层级下拉
        self.assertIn('id="cognition-level"', self.html)
        self.assertIn('<option value="explicit">', self.html)
        self.assertIn('<option value="inductive">', self.html)
        self.assertIn("COGNITIVE_LEVEL_LABELS", self.js)
        # action 提示与强化确认路径
        self.assertIn("cognition-action-hint", self.html)
        self.assertIn("confirmCognitiveReinforce", self.js)
        self.assertIn("强化×", self.js)
        self.assertIn("取代 #", self.js)
        # 多卡渲染不再依赖单条 find
        self.assertIn("filter(candidate => candidate.cognitive_type", self.js)

    def test_dashboard_records_and_shows_human_decisions(self):
        # 拒绝按钮 + 修订历史条
        self.assertIn("cognition-revision-log", self.html)
        self.assertIn("renderCognitiveRevisions", self.js)
        self.assertIn("/api/cognitive-items/revisions", self.js)
        self.assertIn("rejectCognitiveDraft", self.js)
        self.assertIn("/api/cognitive-items/draft/reject", self.js)
        self.assertIn("回喂", self.js)

    def test_dashboard_can_delete_single_revision_record(self):
        # 每条审计记录都有单独删除按钮，从证据回喂中移除
        self.assertIn("/api/cognitive-items/revisions/${rev.id}", self.js)
        self.assertIn("从证据回喂中移除这条记录", self.js)
        self.assertIn("cognition-revision-list li", self.css)


if __name__ == "__main__":
    unittest.main()
