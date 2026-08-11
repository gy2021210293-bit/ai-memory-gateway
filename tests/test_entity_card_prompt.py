import ast
import asyncio
import json
import logging
import sys
import types
import unittest
from pathlib import Path


asyncpg = types.ModuleType("asyncpg")
asyncpg.Pool = object
sys.modules.setdefault("asyncpg", asyncpg)

jieba = types.ModuleType("jieba")
jieba.__path__ = []
jieba.logging = logging
jieba.setLogLevel = lambda _level: None
jieba.add_word = lambda _word: None
jieba_analyse = types.ModuleType("jieba.analyse")
jieba_analyse.extract_tags = lambda text, topK=10: [text]
jieba.analyse = jieba_analyse
sys.modules.setdefault("jieba", jieba)
sys.modules.setdefault("jieba.analyse", jieba_analyse)

import database


ROOT = Path(__file__).resolve().parents[1]

PROMPT_SYMBOLS = {
    "_format_matched_entity_overview",
    "_classify_entity_query",
    "ENTITY_SPECIFIC_QUERY_KEYWORDS",
}


def _load_prompt_functions():
    """AST-extract the injection helpers from main.py (avoids importing FastAPI deps)."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in PROMPT_SYMBOLS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in PROMPT_SYMBOLS:
                    nodes.append(node)
    namespace = {"json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


class EntityCardPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_prompt_functions()

    def test_classify_specific_vs_rest(self):
        classify = self.ns["_classify_entity_query"]
        # 具体（原话/日期）问题 → True，跳过卡片
        self.assertTrue(classify("她当时说的原话是什么"))
        self.assertTrue(classify("具体是哪一天去的"))
        # 历史/普通问题不再区分 → False，统一注入卡片
        self.assertFalse(classify("他以前是怎么发展的"))
        self.assertFalse(classify("他们当时经历了什么"))
        self.assertFalse(classify("她最近怎么样"))
        self.assertFalse(classify(""))

    def test_candidate_entities_never_rendered(self):
        rendered = self.ns["_format_matched_entity_overview"]([{
            "matched_entities": [
                {"id": 1, "name": "Candidate", "type": "person", "retrieval_status": "candidate"},
                {"id": 2, "name": "Active", "type": "person", "retrieval_status": "active",
                 "entity_card_json": None, "description": "", "aliases": [], "exact_name_match": True},
            ],
        }], "")
        self.assertNotIn("Candidate", rendered)
        self.assertIn("Active", rendered)

    def test_legacy_profile_fields_never_injected(self):
        entity = {
            "id": 2, "name": "Alice", "type": "person", "retrieval_status": "active",
            "aliases": ["小艾"], "description": "手工确认说明", "exact_name_match": True,
            "entity_card_json": {
                "description": "手写卡说明",
                "snapshots": [
                    {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00",
                     "state": "住在杭州", "source": "direct"},
                    {"fact_date": "2026-07-20", "recorded_at": "2026-07-20T00:00:00",
                     "state": "搬到上海", "source": "direct"},
                ],
            },
            "profile_json": {
                "summary": "旧概况摘要", "relationship": "旧关系", "stable_facts": ["旧稳定事实"],
                "recent_updates": ["旧近期动态"], "preferences": ["旧偏好"], "uncertainties": ["旧待确认"],
            },
        }
        rendered = self.ns["_format_matched_entity_overview"]([{"matched_entities": [entity]}], "她最近怎么样")
        self.assertIn("手写卡说明", rendered)
        self.assertIn("2026-07-20：搬到上海", rendered)
        self.assertIn("2026-07-01：住在杭州", rendered)
        for forbidden in ("旧概况摘要", "旧关系", "旧稳定事实", "旧近期动态", "旧偏好", "旧待确认"):
            self.assertNotIn(forbidden, rendered)

    def test_history_question_injects_last_three_snapshots(self):
        entity = {
            "id": 2, "name": "项目", "type": "project", "retrieval_status": "active",
            "aliases": [], "description": "", "exact_name_match": True,
            "entity_card_json": {
                "description": "长期项目",
                "snapshots": [
                    {"fact_date": f"2026-0{month}-01", "recorded_at": "", "state": f"阶段{month}", "source": "direct"}
                    for month in range(1, 7)
                ],
            },
        }
        rendered = self.ns["_format_matched_entity_overview"]([{"matched_entities": [entity]}], "这个项目以前是怎么发展的")
        self.assertIn("阶段6", rendered)   # 最近3条快照
        self.assertIn("阶段4", rendered)
        self.assertNotIn("阶段1", rendered)  # 更早快照不注入
        self.assertNotIn("阶段2", rendered)

    def test_ambiguous_match_labels_latest_snapshot(self):
        entity = {
            "id": 2, "name": "Alice", "type": "person", "retrieval_status": "active",
            "aliases": ["小艾"], "description": "", "exact_name_match": False,
            "entity_card_json": {
                "description": "",
                "snapshots": [
                    {"fact_date": "2026-07-20", "recorded_at": "2026-07-20T00:00:00",
                     "state": "住在上海", "source": "direct"},
                ],
            },
        }
        rendered = self.ns["_format_matched_entity_overview"]([{"matched_entities": [entity]}], "她最近怎么样")
        self.assertIn("2026-07-20：住在上海", rendered)
        self.assertIn("不确定是否仍为最新", rendered)

    def test_specific_question_skips_card(self):
        entity = {
            "id": 2, "name": "Alice", "type": "person", "retrieval_status": "active",
            "aliases": [], "description": "", "exact_name_match": True,
            "entity_card_json": {"description": "朋友", "snapshots": [
                {"fact_date": "2026-07-20", "recorded_at": "", "state": "住在上海", "source": "direct"},
            ]},
        }
        rendered = self.ns["_format_matched_entity_overview"]([{"matched_entities": [entity]}], "她当时说的原话是什么")
        self.assertEqual(rendered.strip(), "")


class EntityCardSearchSqlTests(unittest.TestCase):
    def test_entity_search_sql_carries_card_column(self):
        calls = []

        async def fake_fetch(sql, *args):
            calls.append((sql, args))
            return []

        conn = types.SimpleNamespace(fetch=fake_fetch)
        asyncio.run(database._fetch_entity_search_candidates(conn, "小艾", ["小艾"], 10))
        self.assertEqual(len(calls), 1)
        self.assertIn("e.entity_card_json", calls[0][0])


if __name__ == "__main__":
    unittest.main()
