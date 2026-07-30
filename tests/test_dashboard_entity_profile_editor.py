import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DashboardEntityProfileEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

    def test_all_profile_fields_have_editors(self):
        for element_id in (
            "entity-profile-summary-edit",
            "entity-profile-relationship-edit",
            "entity-profile-stable-facts-edit",
            "entity-profile-recent-updates-edit",
            "entity-profile-preferences-edit",
            "entity-profile-uncertainties-edit",
            "entity-profile-evidence-edit",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_existing_profile_can_open_without_generating_a_draft(self):
        self.assertIn('onclick="startEntityProfileEdit()"', self.html)
        self.assertIn("function startEntityProfileEdit()", self.javascript)
        self.assertIn("collectEntityProfileEditor()", self.javascript)

    def test_evidence_ids_are_visible_and_checked_against_entity_memories(self):
        self.assertIn("`#${memory.id} ${memory.content}`", self.javascript)
        self.assertIn("selectedEntityMemoryIds.has(id)", self.javascript)

    def test_dashboard_script_cache_version_is_bumped(self):
        self.assertIn("/static/js/dashboard.js?v=4.9", self.html)
        self.assertIn("/static/css/dashboard.css?v=6.0", self.html)

    def test_editor_collapses_to_one_column_on_narrow_screens(self):
        self.assertIn('class="entity-profile-editor-grid"', self.html)
        self.assertIn(".entity-profile-editor-grid", self.css)
        self.assertIn("grid-template-columns: 1fr;", self.css)


if __name__ == "__main__":
    unittest.main()
