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
        self.assertEqual(self.html.count("onclick=\"generateCognitiveDraft()\""), 1)
        self.assertNotIn("generateCognitiveDraft('user')", self.html)
        self.assertIn("id=\"cognition-review-after\"", self.html)

    def test_dashboard_renders_four_fixed_sections_and_diff(self):
        for cognitive_type in (
            "user_core", "self_core", "relationship_core", "current_field"
        ):
            self.assertIn(f"cognitive_type: '{cognitive_type}'", self.js)
        self.assertIn("cognition-diff-grid", self.js)
        self.assertIn("可能过时", self.js)
        self.assertNotIn("user_traits_preferences", self.js)

    def test_cognition_layout_is_responsive_without_fixed_min_width(self):
        self.assertIn(".cognition-grid", self.css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", self.css)
        self.assertIn("min-width: 0", self.css)


if __name__ == "__main__":
    unittest.main()
