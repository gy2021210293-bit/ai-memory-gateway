import logging
import sys
import types
import unittest


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


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


class EntitySearchTests(unittest.IsolatedAsyncioTestCase):
    def test_all_zero_scores_stay_zero(self):
        self.assertEqual(database._min_max_normalize({1: 0.0, 2: 0.0}), {1: 0.0, 2: 0.0})

    async def test_alias_match_aggregates_all_entity_memory_layers(self):
        rows = [
            {
                "id": 11, "content": "Alice likes jazz", "importance": 6,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 1, "title": None, "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
                "aliases": ["小艾"], "normalized_aliases": ["小艾"],
            },
            {
                "id": 12, "content": "Alice visited Shanghai", "importance": 8,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 2, "title": "Shanghai trip", "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
                "aliases": ["小艾"], "normalized_aliases": ["小艾"],
            },
            {
                "id": 13, "content": "Alice is an important friend", "importance": 10,
                "created_at": database.datetime.now(database.dt_timezone.utc),
                "layer": 3, "title": "Important relationship", "entity_id": 7, "entity_name": "Alice",
                "normalized_name": "alice", "entity_type": "person", "description": "Friend",
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


if __name__ == "__main__":
    unittest.main()
