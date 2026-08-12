import logging
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock


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
import memory_extractor


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


class FakeContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakeEntityConnection:
    def __init__(self, fetchrows=None, execute_result="DELETE 1"):
        self.fetchrows = list(fetchrows or [])
        self.execute_result = execute_result
        self.calls = []

    def transaction(self):
        return FakeContext()

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrows.pop(0) if self.fetchrows else None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_result


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeContext(self.conn)


class EntitySearchTests(unittest.IsolatedAsyncioTestCase):
    def test_message_time_uses_configured_timezone(self):
        rendered = memory_extractor._format_message_time(
            datetime(2026, 7, 18, 16, 30, tzinfo=timezone.utc),
            datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        self.assertIn("2026-07-19 00:30", rendered)
        self.assertIn("UTC+0800", rendered)

    def test_message_time_falls_back_for_invalid_input(self):
        fallback = datetime(2026, 7, 18, 2, 5, tzinfo=timezone.utc)
        rendered = memory_extractor._format_message_time("not-a-time", fallback)
        self.assertIn("2026-07-18 10:05", rendered)

    def test_user_is_not_an_entity(self):
        self.assertTrue(database.is_user_entity_name("晏晏"))
        self.assertTrue(database.is_user_entity_name(" USER "))
        self.assertEqual(
            memory_extractor._exclude_user_entities([
                {"name": "晏晏", "type": "person"},
                {"name": "Alice", "type": "person"},
            ]),
            [{"name": "Alice", "type": "person"}],
        )

    def test_ai_participants_are_not_entities(self):
        for name in ("Huxley", "栖", "向野"):
            self.assertTrue(database.is_excluded_entity_name(name))
        self.assertEqual(
            memory_extractor._exclude_user_entities([
                {"name": "Huxley", "type": "person"},
                {"name": "栖", "type": "person"},
                {"name": "向野", "type": "person"},
                {"name": "Alice", "type": "person"},
            ]),
            [{"name": "Alice", "type": "person"}],
        )

    def test_code_identifiers_and_files_are_not_entities(self):
        entities = [
            {"name": "home_garden_blind_chest", "type": "other", "confidence": 0.99},
            {"name": "render_page()", "type": "other", "confidence": 0.99},
            {"name": "DATABASE_URL", "type": "other", "confidence": 0.99},
            {"name": "main.py", "type": "other", "confidence": 0.99},
            {"name": "src/utils.js", "type": "other", "confidence": 0.99},
            {"name": "Alice", "type": "person", "confidence": 0.99},
            {"name": "上海", "type": "place", "confidence": 0.95},
        ]
        self.assertEqual(
            [item["name"] for item in memory_extractor._exclude_user_entities(entities)],
            ["Alice", "上海"],
        )

    def test_low_confidence_entity_is_not_persisted(self):
        self.assertEqual(
            memory_extractor._exclude_user_entities([
                {"name": "可能只是术语", "type": "other", "confidence": 0.4},
                {"name": "长期项目", "type": "project", "confidence": 0.85},
            ]),
            [{"name": "长期项目", "type": "project", "confidence": 0.85}],
        )

    def test_all_zero_scores_stay_zero(self):
        self.assertEqual(database._min_max_normalize({1: 0.0, 2: 0.0}), {1: 0.0, 2: 0.0})

    async def test_alias_match_aggregates_all_entity_memory_layers(self):
        rows = [
            {
                "id": 11, "content": "Alice likes jazz", "importance": 6,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 1, "title": None, "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
                "profile_json": None,
                "aliases": ["小艾"], "normalized_aliases": ["小艾"],
            },
            {
                "id": 12, "content": "Alice visited Shanghai", "importance": 8,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 2, "title": "Shanghai trip", "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
                "profile_json": None,
                "aliases": ["小艾"], "normalized_aliases": ["小艾"],
            },
            {
                "id": 13, "content": "Alice is an important friend", "importance": 10,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 3, "title": "Important relationship", "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
                "profile_json": None,
                "aliases": ["小艾"], "normalized_aliases": ["小艾"],
            },
        ]
        conn = FakeConnection(rows)

        candidates = await database._fetch_entity_search_candidates(conn, "小艾最近怎么样", ["小艾"], 10)

        self.assertEqual(set(candidates), {11, 12, 13})
        self.assertEqual({item["layer"] for item in candidates.values()}, {1, 2, 3})
        self.assertTrue(all(item["matched_entities"][0]["name"] == "Alice" for item in candidates.values()))
        terms = conn.calls[0][1][0]
        self.assertIn("小艾", terms)
        sql = conn.calls[0][0]
        self.assertIn("e.status_override = 'active'", sql)
        self.assertIn("e.evidence_count >= $3", sql)
        self.assertNotIn("term LIKE", sql)
        self.assertIn("entity_rank <= 3", sql)

    async def test_direct_mentions_refresh_dormant_entities_without_memory_ranking(self):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        conn = FakeEntityConnection()
        conn.fetchrows = []
        conn.fetch = AsyncMock(return_value=[
            {
                "id": 7, "name": "Alice", "normalized_name": "alice", "entity_type": "person",
                "description": "朋友", "profile_json": None, "entity_card_json": {},
                "evidence_count": 5, "status_override": None, "last_referenced_at": None,
                "last_evidence_at": old, "aliases": ["Ally"], "normalized_aliases": ["ally"],
            },
            {
                "id": 8, "name": "Bob", "normalized_name": "bob", "entity_type": "person",
                "description": "同事", "profile_json": None, "entity_card_json": {},
                "evidence_count": 5, "status_override": None, "last_referenced_at": None,
                "last_evidence_at": old, "aliases": [], "normalized_aliases": [],
            },
            {
                "id": 9, "name": "Candidate", "normalized_name": "candidate", "entity_type": "person",
                "description": "", "profile_json": None, "entity_card_json": {},
                "evidence_count": 1, "status_override": None, "last_referenced_at": None,
                "last_evidence_at": old, "aliases": [], "normalized_aliases": [],
            },
        ])
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            result = await database.find_directly_mentioned_entities("Alice 和 Bob 最近怎么样")
        finally:
            database.get_pool = old_get_pool
        self.assertEqual([item["name"] for item in result], ["Alice", "Bob"])
        self.assertTrue(all(item["retrieval_status"] == "active" for item in result))
        execute_calls = [call for call in conn.calls if call[0] == "execute"]
        self.assertEqual(execute_calls[0][2][0], [7, 8])
        self.assertIn("last_referenced_at = NOW()", execute_calls[0][1])

    def test_profile_normalization_rejects_unknown_evidence(self):
        profile = memory_extractor.normalize_entity_profile({
            "summary": "  Alice is a friend.  ",
            "relationship": "friend",
            "stable_facts": ["Likes jazz"],
            "recent_updates": [],
            "preferences": ["Jazz"],
            "uncertainties": ["Current city is unclear"],
            "evidence_memory_ids": [11, "12", 999, "invalid"],
        }, {11, 12})

        self.assertEqual(profile["summary"], "Alice is a friend.")
        self.assertEqual(profile["evidence_memory_ids"], [11, 12])
        self.assertNotIn(999, profile["evidence_memory_ids"])

    def test_profile_normalization_is_compact(self):
        profile = memory_extractor.normalize_entity_profile({
            "summary": "我" * 250,
            "relationship": "朋友" * 100,
            "stable_facts": ["事实" * 100] * 8,
            "evidence_memory_ids": [1],
        }, {1})

        self.assertEqual(len(profile["summary"]), 200)
        self.assertEqual(len(profile["relationship"]), 120)
        self.assertEqual(len(profile["stable_facts"]), 6)
        self.assertTrue(all(len(item) <= 80 for item in profile["stable_facts"]))

    def test_entity_update_normalizes_and_deduplicates_aliases(self):
        value = database.normalize_entity_update({
            "name": "  Alice  ", "entity_type": "PERSON",
            "aliases": [" Ally ", "ally", "Alice", ""],
        })
        self.assertEqual(value["name"], "Alice")
        self.assertEqual(value["entity_type"], "person")
        self.assertEqual(value["aliases"], [{"alias": "Ally", "normalized_alias": "ally"}])

    async def test_entity_rename_collision_requires_merge(self):
        conn = FakeEntityConnection(fetchrows=[{"id": 1}, {"id": 2, "name": "Alice"}])
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            result = await database.update_entity(1, {"name": "Alice", "entity_type": "person", "aliases": []})
        finally:
            database.get_pool = old_get_pool
        self.assertIn("合并实体", result["error"])
        self.assertFalse(any(call[0] == "execute" for call in conn.calls))

    async def test_entity_alias_cannot_use_another_entity_name(self):
        conn = FakeEntityConnection(fetchrows=[
            {"id": 1}, None, None, {"id": 2, "name": "Alice"},
        ])
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            result = await database.update_entity(1, {
                "name": "Bob", "entity_type": "person", "aliases": [" alice "],
            })
        finally:
            database.get_pool = old_get_pool
        self.assertIn("冲突", result["error"])
        self.assertFalse(any(call[0] == "execute" for call in conn.calls))

    async def test_delete_entity_only_deletes_entity_row(self):
        conn = FakeEntityConnection()
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            result = await database.delete_entity(7)
        finally:
            database.get_pool = old_get_pool
        self.assertEqual(result["status"], "ok")
        execute_calls = [call for call in conn.calls if call[0] == "execute"]
        self.assertEqual(len(execute_calls), 1)
        self.assertIn("DELETE FROM entities", execute_calls[0][1])
        self.assertNotIn("memories", execute_calls[0][1])


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
