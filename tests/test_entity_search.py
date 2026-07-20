import logging
import sys
import types
import unittest
from datetime import datetime, timezone


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


if __name__ == "__main__":
    unittest.main()
