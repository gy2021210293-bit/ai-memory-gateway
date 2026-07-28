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


class FakeContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakeConnection:
    def __init__(self, deleted_rows):
        self.deleted_rows = deleted_rows
        self.calls = []

    def transaction(self):
        return FakeContext()

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.deleted_rows

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "DELETE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeContext(self.conn)


async def _async_value(value):
    return value


class ConversationDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_connection(self, conn, operation):
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            return await operation()
        finally:
            database.get_pool = old_get_pool

    async def test_single_delete_reports_when_no_conversation_matched(self):
        conn = FakeConnection([])
        deleted = await self.run_with_connection(conn, lambda: database.delete_conversation("missing"))
        self.assertFalse(deleted)

    async def test_single_delete_removes_conversation_cache_and_token_usage(self):
        conn = FakeConnection([{"id": 1}, {"id": 2}])
        deleted = await self.run_with_connection(conn, lambda: database.delete_conversation("session-a"))
        self.assertTrue(deleted)
        sql = "\n".join(call[1] for call in conn.calls)
        self.assertIn("DELETE FROM conversations", sql)
        self.assertIn("DELETE FROM session_cache_state", sql)
        self.assertIn("DELETE FROM memory_extraction_state", sql)
        self.assertIn("DELETE FROM token_usage", sql)

    async def test_batch_delete_returns_real_distinct_session_count(self):
        conn = FakeConnection([
            {"session_id": "session-a"}, {"session_id": "session-a"}, {"session_id": "session-b"},
        ])
        deleted = await self.run_with_connection(
            conn, lambda: database.batch_delete_conversations(["session-a", "session-b"]),
        )
        self.assertEqual(deleted, 2)
        self.assertTrue(all("ANY($1::text[])" in call[1] for call in conn.calls))


if __name__ == "__main__":
    unittest.main()
