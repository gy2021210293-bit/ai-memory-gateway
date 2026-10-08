import logging
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
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


class LinkConnection:
    def __init__(self, existing_source=None):
        self.existing_source = existing_source
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, _sql, *_args):
        return {"id": 7}

    async def fetchval(self, _sql, *_args):
        return self.existing_source

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class DeleteConnection:
    def __init__(self):
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetch(self, _sql, *_args):
        return [{"entity_id": 7, "removed": 1}]

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "DELETE 1" if "DELETE FROM memories" in sql else "UPDATE 1"


class MergeConnection:
    def __init__(self):
        self.rows = [
            {"id": 1, "name": "Ally", "normalized_name": "ally", "evidence_count": 2},
            {"id": 2, "normalized_name": "ally", "evidence_count": 3},
        ]
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, _sql, *_args):
        return self.rows.pop(0)

    async def fetchval(self, _sql, *_args):
        return 1

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class EntityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_lifecycle_precedence(self):
        self.assertEqual(
            database.attach_entity_lifecycle({"evidence_count": 2})["retrieval_status"],
            "candidate",
        )
        evidence = database.attach_entity_lifecycle({"evidence_count": 3})
        self.assertEqual((evidence["retrieval_status"], evidence["status_source"]), ("active", "evidence"))
        profile = database.attach_entity_lifecycle({"evidence_count": 0, "profile_json": {"summary": "known"}})
        self.assertEqual((profile["retrieval_status"], profile["status_source"]), ("active", "profile"))
        manual = database.attach_entity_lifecycle({
            "evidence_count": 9,
            "profile_json": {"summary": "known"},
            "status_override": "candidate",
        })
        self.assertEqual((manual["retrieval_status"], manual["status_source"]), ("candidate", "manual"))

    def test_dormant_derivation_no_exemption(self):
        """休眠对所有实体一视同仁（人工设 active / profile 都无豁免）。"""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        # 证据够但很久没被提到 → dormant
        stale = database.attach_entity_lifecycle({"evidence_count": 5, "last_evidence_at": old})
        self.assertEqual((stale["retrieval_status"], stale["status_source"]), ("dormant", "stale_evidence"))
        # 证据够且最近被提到 → active
        fresh = database.attach_entity_lifecycle({"evidence_count": 5, "last_evidence_at": recent})
        self.assertEqual((fresh["retrieval_status"], fresh["status_source"]), ("active", "evidence"))
        # 人工设 active 同样休眠（无豁免）
        manual = database.attach_entity_lifecycle({
            "evidence_count": 5, "last_evidence_at": old, "status_override": "active",
        })
        self.assertEqual((manual["retrieval_status"], manual["status_source"]), ("dormant", "stale_evidence"))
        # profile 实体同样休眠（无豁免）
        profile = database.attach_entity_lifecycle({
            "evidence_count": 0, "profile_json": {"summary": "known"}, "last_evidence_at": old,
        })
        self.assertEqual((profile["retrieval_status"], profile["status_source"]), ("dormant", "stale_evidence"))
        # 无 last_evidence_at → 保守视为 recent，不误杀
        undated = database.attach_entity_lifecycle({"evidence_count": 3})
        self.assertEqual(undated["retrieval_status"], "active")
        # 阈值边界：89 天前 → 仍活跃；91 天前 → 休眠
        boundary_fresh = database.attach_entity_lifecycle({
            "evidence_count": 5, "last_evidence_at": (now - timedelta(days=89)).isoformat(),
        })
        self.assertEqual(boundary_fresh["retrieval_status"], "active")
        boundary_stale = database.attach_entity_lifecycle({
            "evidence_count": 5, "last_evidence_at": (now - timedelta(days=91)).isoformat(),
        })
        self.assertEqual(boundary_stale["retrieval_status"], "dormant")

    def test_entity_name_matching_rejects_short_and_partial_latin_names(self):
        self.assertEqual(
            database._entity_name_matches_query("星", "星怎么样", ["星"]),
            (False, False),
        )
        self.assertEqual(
            database._entity_name_matches_query("Ali", "alice最近怎么样", ["alice最近怎么样"]),
            (False, False),
        )
        self.assertEqual(
            database._entity_name_matches_query("Alice", "alice最近怎么样", ["alice最近怎么样"]),
            (False, True),
        )
        self.assertEqual(
            database._entity_name_matches_query("小艾", "小艾最近怎么样", ["小艾"]),
            (True, True),
        )

    async def test_new_original_link_increments_evidence_once(self):
        conn = LinkConnection(existing_source=None)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            await database.link_memory_entities(
                11, [{"name": "Alice", "type": "person", "confidence": 0.9}]
            )
        evidence_updates = [
            args for sql, args in conn.execute_calls
            if "evidence_count = evidence_count + 1" in sql
        ]
        self.assertEqual(evidence_updates, [(7,)])

    async def test_duplicate_or_inherited_link_does_not_increment(self):
        for existing_source, source in (("extractor", "extractor"), (None, "inherited")):
            conn = LinkConnection(existing_source=existing_source)
            with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
                await database.link_memory_entities(
                    11, [{"name": "Alice", "type": "person", "confidence": 0.9}], source=source
                )
            self.assertFalse(any(
                "evidence_count = evidence_count + 1" in sql
                for sql, _args in conn.execute_calls
            ))

    async def test_original_link_replacing_inherited_link_increments_once(self):
        conn = LinkConnection(existing_source="inherited")
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            await database.link_memory_entities(
                11, [{"name": "Alice", "type": "person", "confidence": 0.9}]
            )
        self.assertEqual(sum(
            "evidence_count = evidence_count + 1" in sql
            for sql, _args in conn.execute_calls
        ), 1)

    async def test_explicit_delete_decrements_evidence(self):
        conn = DeleteConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            await database.delete_memory(11)
        self.assertTrue(any(
            "evidence_count = GREATEST(0, evidence_count - $2)" in sql
            for sql, _args in conn.execute_calls
        ))

    async def test_fragment_cleanup_does_not_decrement_evidence(self):
        conn = DeleteConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            deleted = await database.cleanup_old_fragments(30)
        self.assertEqual(deleted, 1)
        self.assertFalse(any("UPDATE entities" in sql for sql, _args in conn.execute_calls))

    async def test_low_importance_cleanup_dry_run_only_counts(self):
        class DryRunConnection:
            def transaction(self):
                return AsyncContext()

            async def fetchval(self, _sql, *_args):
                return 3

            async def fetch(self, _sql, *_args):
                return []

            async def execute(self, sql, *args):
                raise AssertionError(f"dry_run 不应执行写入: {sql}")

        conn = DryRunConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            deleted = await database.cleanup_low_importance_fragments(14, 3, dry_run=True)
        self.assertEqual(deleted, 3)

    async def test_low_importance_cleanup_decrements_evidence_and_deletes(self):
        class LowImportanceConnection:
            def __init__(self):
                self.execute_calls = []

            def transaction(self):
                return AsyncContext()

            async def fetchval(self, _sql, *_args):
                return 2

            async def fetch(self, _sql, *_args):
                return [{"entity_id": 7, "removed": 1}]

            async def execute(self, sql, *args):
                self.execute_calls.append((sql, args))
                import re
                placeholders = [int(m) for m in re.findall(r"\$(\d+)", sql)]
                if placeholders:
                    assert max(placeholders) <= len(args), f"Placeholder ${max(placeholders)} out of range for {len(args)} args in query: {sql}"
                return "DELETE 2" if "DELETE FROM memories" in sql else "UPDATE 1"

        conn = LowImportanceConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            deleted = await database.cleanup_low_importance_fragments(14, 3)
        self.assertEqual(deleted, 2)
        self.assertTrue(any(
            "evidence_count = GREATEST(0, evidence_count - $2)" in sql
            for sql, _args in conn.execute_calls
        ))
        self.assertTrue(any(
            "DELETE FROM memories" in sql and "is_active = TRUE" in sql
            for sql, _args in conn.execute_calls
        ))

    async def test_status_override_validation_and_readback(self):
        self.assertIn("error", await database.set_entity_status(7, "invalid"))
        conn = DeleteConnection()
        with (
            patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))),
            patch.object(database, "get_entity_detail", AsyncMock(return_value={
                "id": 7, "retrieval_status": "active"
            })),
        ):
            result = await database.set_entity_status(7, "active")
        self.assertEqual(result["entity"]["retrieval_status"], "active")

    async def test_merge_combines_counts_and_subtracts_visible_overlap(self):
        conn = MergeConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.merge_entities(1, 2)
        self.assertEqual(result["status"], "ok")
        count_updates = [
            args for sql, args in conn.execute_calls
            if "SET evidence_count = $2" in sql
        ]
        self.assertEqual(count_updates, [(2, 4)])


if __name__ == "__main__":
    unittest.main()
