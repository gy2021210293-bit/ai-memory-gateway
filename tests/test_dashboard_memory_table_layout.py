import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DashboardMemoryTableLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

    def test_desktop_table_can_shrink_to_its_container(self):
        self.assertIn("table-layout: fixed;", self.css)
        self.assertIn("min-width: 0;", self.css)
        self.assertNotIn("min-width: 1040px;", self.css)

    def test_wide_editing_columns_no_longer_have_large_minimums(self):
        self.assertIn(".data-table .col-layer", self.css)
        self.assertIn("width: 64px;", self.css)
        self.assertIn(".data-table .col-title", self.css)
        self.assertNotIn("min-width: 210px;", self.css)
        self.assertIn(".data-table .col-content", self.css)
        self.assertNotIn("min-width: 430px;", self.css)

    def test_dashboard_css_cache_version_is_bumped(self):
        self.assertIn("/static/css/dashboard.css?v=5.9", self.html)


if __name__ == "__main__":
    unittest.main()
