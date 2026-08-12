import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DashboardEntityCardEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

    def test_entity_card_ui_elements_are_present(self):
        for element_id in (
            "entity-card-description-edit",
            "entity-card-snapshots",
            "entity-card-proposals",
            "entity-card-snapshot-state",
            "entity-card-snapshot-date",
            "entity-card-snapshot-memory",
            "entity-card-snapshot-message",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_entity_card_js_actions_exist(self):
        for function_name in (
            "function loadEntityCard(",
            "function saveEntityCardDescription(",
            "function addEntityCardSnapshot(",
            "function decideEntityCardProposal(",
            "function renderEntityCardSnapshots(",
            "function renderEntityCardProposals(",
            "function startEditEntityCardSnapshot(",
            "function saveEntityCardSnapshot(",
            "function deleteEntityCardSnapshot(",
        ):
            self.assertIn(function_name, self.javascript)

    def test_generate_profile_flow_removed(self):
        self.assertNotIn("生成/刷新概况", self.html)
        self.assertNotIn("generateEntityProfileDraft", self.javascript)
        self.assertNotIn("entity-profile-summary-edit", self.html)
        self.assertNotIn("entity-profile-editor-grid", self.html)
        self.assertNotIn("startEntityProfileEdit", self.javascript)

    def test_legacy_profile_is_readonly_but_present(self):
        self.assertIn("遗留概况", self.html)
        self.assertIn('id="entity-profile-current"', self.html)
        self.assertIn("function formatEntityProfile(", self.javascript)

    def test_backfill_cards_button_and_action_exist(self):
        self.assertIn("补全实体状态卡", self.html)
        self.assertIn("function backfillEntityCards(", self.javascript)
        self.assertIn("/api/entities/backfill-cards", self.javascript)
        self.assertIn("/api/entities/backfill-cards/status", self.javascript)
        self.assertIn("待确认提案", self.javascript)

    def test_card_styles_exist(self):
        self.assertIn(".entity-card-snapshot", self.css)
        self.assertIn(".entity-card-proposal", self.css)

    def test_dashboard_script_cache_version_is_bumped(self):
        self.assertIn("/static/js/dashboard.js?v=6.9", self.html)
        self.assertIn("/static/css/dashboard.css?v=7.4", self.html)

    def test_entity_relation_editor_and_actions_exist(self):
        for element_id in (
            "entity-relations-list",
            "entity-relation-other",
            "entity-relation-text",
            "entity-relation-save",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        for function_name in (
            "function loadEntityRelations(",
            "function saveEntityRelation(",
            "function suppressEntityRelation(",
            "function restoreEntityRelation(",
        ):
            self.assertIn(function_name, self.javascript)
        self.assertIn(".entity-relation-item", self.css)

    def test_snapshot_edit_delete_endpoints_and_ui_exist(self):
        self.assertIn("/card/snapshots", self.javascript)
        self.assertIn("method: 'PUT'", self.javascript)
        self.assertIn("method: 'DELETE'", self.javascript)
        self.assertIn("entity-card-snapshot-actions", self.javascript)
        self.assertIn("entity-card-snapshot-edit", self.javascript)

    def test_entity_list_shows_card_and_proposal_badges(self):
        self.assertIn("entity-item-badges", self.javascript)
        self.assertIn("pending_proposal_count", self.javascript)
        self.assertIn("card_last_state_date", self.javascript)
        self.assertIn(".entity-badge-pending", self.css)
        self.assertIn(".entity-badge-card", self.css)
        self.assertIn(".entity-badge-date", self.css)

    def test_conversation_detail_uses_query_parameter_session_id(self):
        self.assertIn("/api/conversation-messages?", self.javascript)
        self.assertIn("session_id: sessionId", self.javascript)
        self.assertIn("if (!resp.ok)", self.javascript)


if __name__ == "__main__":
    unittest.main()
