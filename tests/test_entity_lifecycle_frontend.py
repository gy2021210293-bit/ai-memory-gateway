import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntityLifecycleFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard_js = (ROOT / "static/js/dashboard.js").read_text(encoding="utf-8")
        cls.constellation_js = (ROOT / "static/constellation/data.js").read_text(encoding="utf-8")
        cls.dashboard_html = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")

    def test_dashboard_groups_active_and_candidate_entities(self):
        self.assertIn("renderGroup('活跃实体'", self.dashboard_js)
        self.assertIn("renderGroup('候选实体'", self.dashboard_js)
        self.assertIn("entity.evidence_count", self.dashboard_js)

    def test_dashboard_exposes_manual_status_actions(self):
        self.assertIn("setEntityRetrievalStatus('active')", self.dashboard_html)
        self.assertIn("setEntityRetrievalStatus('candidate')", self.dashboard_html)
        self.assertIn("setEntityRetrievalStatus('auto')", self.dashboard_html)
        self.assertIn("/status", self.dashboard_js)

    def test_constellation_only_groups_active_entities(self):
        self.assertIn("entity.retrieval_status === 'active'", self.constellation_js)

    def test_prompt_overview_excludes_candidate_entities(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = {
            "_format_matched_entity_overview",
            "_classify_entity_query",
            "ENTITY_SPECIFIC_QUERY_KEYWORDS",
            "ENTITY_HISTORY_QUERY_KEYWORDS",
        }
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                nodes.append(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in wanted:
                        nodes.append(node)
        namespace = {"json": json}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
        rendered = namespace["_format_matched_entity_overview"]([{
            "matched_entities": [
                {"id": 1, "name": "Candidate", "type": "person", "retrieval_status": "candidate"},
                {"id": 2, "name": "Active", "type": "person", "retrieval_status": "active",
                 "entity_card_json": None, "description": "", "aliases": [], "exact_name_match": True},
            ]
        }])
        self.assertNotIn("Candidate", rendered)
        self.assertIn("Active", rendered)


if __name__ == "__main__":
    unittest.main()
