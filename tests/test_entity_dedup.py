import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


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


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


class ResolutionConnection:
    """Fake conn for link_memory_entities resolution-order tests.

    exact_hit: row returned by the exact-normalized_name SELECT (None = miss).
    insert_id: id returned by the INSERT INTO entities ... RETURNING id.
    roster / aliases: rows returned by the lazy _load_entity_guard fetches.
    """

    def __init__(self, roster=None, aliases=None, exact_hit=None, insert_id=999):
        self.roster = roster or []
        self.aliases = aliases or []
        self.exact_hit = exact_hit
        self.insert_id = insert_id
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, sql, *_args):
        if sql.startswith("SELECT id FROM entities WHERE normalized_name"):
            return self.exact_hit
        if sql.startswith("SELECT ea.entity_id"):
            return None  # alias miss
        if "INSERT INTO entities" in sql:
            return {"id": self.insert_id}
        return None

    async def fetchval(self, _sql, *_args):
        return None

    async def fetch(self, sql, *_args):
        if "FROM entities" in sql and "entity_aliases" not in sql:
            return self.roster
        return self.aliases

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class DuplicateScanConnection:
    def __init__(self, rows):
        self.rows = rows

    def transaction(self):
        return AsyncContext()

    async def fetch(self, _sql, *_args):
        return self.rows


class EntitySurfaceTests(unittest.TestCase):
    def test_normalize_entity_name_nfkc_folds_fullwidth(self):
        self.assertEqual(database.normalize_entity_name("Ａｌｉｃｅ"), "alice")
        self.assertEqual(database.normalize_entity_name("  USER "), "user")
        self.assertEqual(database.normalize_entity_name("Alice"), "alice")

    def test_canonicalize_strips_bracket_annotations(self):
        self.assertEqual(database.canonicalize_entity_surface("Alice（朋友）"), "alice")
        self.assertEqual(database.canonicalize_entity_surface(" 小明 (朋友) "), "小明")

    def test_canonicalize_strips_honorific_only_when_remainder_long(self):
        self.assertEqual(database.canonicalize_entity_surface("小明同学"), "小明")
        self.assertEqual(database.canonicalize_entity_surface("王老师"), "王老师")  # 余下 1 字不剥

    def test_similar_enough_thresholds(self):
        self.assertTrue(database._entity_similar_enough("小明", "小明"))
        self.assertTrue(database._entity_similar_enough("xiaoming", "xiaomingo"))
        self.assertFalse(database._entity_similar_enough("小明", "小红"))
        self.assertFalse(database._entity_similar_enough("alice", "bob"))


class LinkMemoryEntitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_match_reuses_existing_entity(self):
        conn = ResolutionConnection(exact_hit={"id": 7})
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            linked = await database.link_memory_entities(
                11, [{"name": "Alice", "type": "person", "confidence": 1.0}]
            )
        self.assertEqual(linked, 1)
        self.assertFalse(any("INSERT INTO entities" in sql for sql, _ in conn.execute_calls))
        self.assertFalse(any("INSERT INTO entity_aliases" in sql for sql, _ in conn.execute_calls))

    async def test_fuzzy_match_auto_aliases_and_links_existing(self):
        conn = ResolutionConnection(
            roster=[{"id": 7, "name": "小明", "normalized_name": "小明", "entity_type": "person"}],
        )
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            linked = await database.link_memory_entities(
                11, [{"name": "小明同学", "type": "person", "confidence": 0.9}]
            )
        self.assertEqual(linked, 1)
        # 新表面写法被自动加为别名（非破坏性），不新建实体行
        alias_inserts = [
            args for sql, args in conn.execute_calls if "INSERT INTO entity_aliases" in sql
        ]
        self.assertEqual(alias_inserts, [(7, "小明同学", "小明同学")])
        memory_links = [
            args for sql, args in conn.execute_calls if "INSERT INTO memory_entities" in sql
        ]
        self.assertEqual(memory_links, [(11, 7, 0.9, "extractor")])
        self.assertFalse(any("INSERT INTO entities" in sql for sql, _ in conn.execute_calls))

    async def test_alias_guard_skips_other_entity_name(self):
        conn = ResolutionConnection(
            exact_hit={"id": 7},
            roster=[
                {"id": 7, "name": "小明", "normalized_name": "小明", "entity_type": "person"},
                {"id": 8, "name": "小红", "normalized_name": "小红", "entity_type": "person"},
            ],
        )
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            linked = await database.link_memory_entities(
                11, [{"name": "小明", "type": "person", "confidence": 0.9, "aliases": ["小红"]}]
            )
        self.assertEqual(linked, 1)
        self.assertFalse(any("INSERT INTO entity_aliases" in sql for sql, _ in conn.execute_calls))

    async def test_new_entity_inserted_when_no_match(self):
        conn = ResolutionConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            linked = await database.link_memory_entities(
                11, [{"name": "张三", "type": "person", "confidence": 0.8}]
            )
        self.assertEqual(linked, 1)
        memory_links = [
            args for sql, args in conn.execute_calls if "INSERT INTO memory_entities" in sql
        ]
        self.assertEqual(memory_links, [(11, 999, 0.8, "extractor")])

    async def test_llm_aliases_persisted_on_new_entity(self):
        conn = ResolutionConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            linked = await database.link_memory_entities(
                11, [{"name": "张三", "type": "person", "confidence": 0.9, "aliases": ["三哥"]}]
            )
        self.assertEqual(linked, 1)
        alias_inserts = [
            args for sql, args in conn.execute_calls if "INSERT INTO entity_aliases" in sql
        ]
        self.assertEqual(alias_inserts, [(999, "三哥", "三哥")])


class FindDuplicateEntitiesTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, conn):
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            return await database.find_duplicate_entities()

    async def test_canonical_group_picks_highest_evidence_target(self):
        groups = await self._run(DuplicateScanConnection([
            {"id": 1, "name": "小明", "normalized_name": "小明", "entity_type": "person", "evidence_count": 5, "memory_count": 2},
            {"id": 2, "name": "小明同学", "normalized_name": "小明同学", "entity_type": "person", "evidence_count": 2, "memory_count": 1},
            {"id": 3, "name": "小红", "normalized_name": "小红", "entity_type": "person", "evidence_count": 4, "memory_count": 1},
        ]))
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["reason"], "canonical")
        self.assertEqual({e["name"] for e in group["entities"]}, {"小明", "小明同学"})
        target = next(e for e in group["entities"] if e["is_target"])
        self.assertEqual(target["name"], "小明")

    async def test_similarity_pair_detected(self):
        groups = await self._run(DuplicateScanConnection([
            {"id": 5, "name": "xiaoming", "normalized_name": "xiaoming", "entity_type": "person", "evidence_count": 3, "memory_count": 1},
            {"id": 6, "name": "xiaomingo", "normalized_name": "xiaomingo", "entity_type": "person", "evidence_count": 1, "memory_count": 1},
            {"id": 7, "name": "上海", "normalized_name": "上海", "entity_type": "place", "evidence_count": 6, "memory_count": 3},
        ]))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["reason"], "similarity")


class RosterPromptTests(unittest.TestCase):
    def test_render_entity_roster_empty(self):
        self.assertEqual(memory_extractor._render_entity_roster(None), "")
        self.assertEqual(memory_extractor._render_entity_roster([]), "")

    def test_render_entity_roster_lists_canonical_names(self):
        text = memory_extractor._render_entity_roster([
            {"name": "小明", "entity_type": "person", "aliases": ["小明同学"]},
            {"name": "上海", "entity_type": "place"},
        ])
        self.assertIn("小明", text)
        self.assertIn("小明同学", text)
        self.assertIn("aliases", text)
        self.assertIn("reuse", text)

    def test_exclude_user_entities_nfkc_catches_fullwidth_excluded_name(self):
        result = memory_extractor._exclude_user_entities([
            {"name": "ＵＳＥＲ", "type": "person", "confidence": 0.99},
        ])
        self.assertEqual(result, [])

    def test_exclude_user_entities_sanitizes_aliases(self):
        result = memory_extractor._exclude_user_entities([
            {"name": "Ａｌｉｃｅ", "type": "person", "confidence": 0.9, "aliases": ["alice", ""]},
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["aliases"], ["alice"])


if __name__ == "__main__":
    unittest.main()
