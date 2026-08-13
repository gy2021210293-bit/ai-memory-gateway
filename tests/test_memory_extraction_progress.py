import ast
import json
import logging
import sys
import types
import unittest
import uuid
from pathlib import Path
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


class FakeContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class ScriptedConnection:
    def __init__(self, fetchrows=None, fetchvals=None, rows=None, execute_result="UPDATE 1"):
        self.fetchrows = list(fetchrows or [])
        self.fetchvals = list(fetchvals or [])
        self.rows = list(rows or [])
        self.execute_result = execute_result
        self.calls = []

    def transaction(self):
        return FakeContext()

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_result

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrows.pop(0)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.fetchvals.pop(0)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeContext(self.conn)


async def _async_value(value):
    return value


class DatabaseExtractionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_connection(self, conn, operation):
        old_get_pool = database.get_pool
        database.get_pool = lambda: _async_value(FakePool(conn))
        try:
            return await operation()
        finally:
            database.get_pool = old_get_pool

    async def test_first_state_uses_existing_message_tail_as_baseline(self):
        conn = ScriptedConnection()

        await self.run_with_connection(
            conn,
            lambda: database.ensure_memory_extraction_state("thread-a"),
        )

        sql, args = conn.calls[0][1], conn.calls[0][2]
        self.assertIn("COALESCE(MAX(id), 0)", sql)
        self.assertIn("ON CONFLICT (session_id) DO NOTHING", sql)
        self.assertEqual(args, ("thread-a",))

    async def test_round_progress_is_persisted_without_claim_before_interval(self):
        conn = ScriptedConnection(fetchrows=[{"pending_rounds": 3}, None])
        result = await self.run_with_connection(
            conn,
            lambda: database.record_memory_extraction_round("thread-a", 20, "claim-a"),
        )

        self.assertEqual(result, {"should_extract": False, "pending_rounds": 3})
        sql = "\n".join(call[1] for call in conn.calls)
        self.assertIn("pending_rounds = pending_rounds + 1", sql)
        self.assertIn("claim_started_at", sql)

    async def test_due_round_claim_captures_cursor_and_message_boundary(self):
        conn = ScriptedConnection(
            fetchrows=[
                {"pending_rounds": 20},
                {"claimed_rounds": 20, "last_extracted_message_id": 41},
            ],
            fetchvals=[88],
        )
        result = await self.run_with_connection(
            conn,
            lambda: database.record_memory_extraction_round("thread-a", 20, "claim-a"),
        )

        self.assertTrue(result["should_extract"])
        self.assertEqual(result["claimed_rounds"], 20)
        self.assertEqual(result["last_extracted_message_id"], 41)
        self.assertEqual(result["through_message_id"], 88)

    async def test_success_advances_cursor_but_failure_release_does_not(self):
        complete_conn = ScriptedConnection()
        completed = await self.run_with_connection(
            complete_conn,
            lambda: database.complete_memory_extraction("thread-a", "claim-a", 88, 20),
        )
        self.assertTrue(completed)
        complete_sql = complete_conn.calls[0][1]
        self.assertIn("last_extracted_message_id = GREATEST", complete_sql)
        self.assertIn("pending_rounds = GREATEST", complete_sql)

        release_conn = ScriptedConnection()
        released = await self.run_with_connection(
            release_conn,
            lambda: database.release_memory_extraction_claim("thread-a", "claim-a"),
        )
        self.assertTrue(released)
        release_sql = release_conn.calls[0][1]
        self.assertNotIn("last_extracted_message_id =", release_sql)
        self.assertNotIn("pending_rounds =", release_sql)

    async def test_message_batch_uses_cursor_bounds_and_keeps_timestamps(self):
        created = object()
        conn = ScriptedConnection(rows=[{
            "id": 42,
            "role": "user",
            "content": "hello",
            "metadata": None,
            "created_at": created,
        }])
        rows = await self.run_with_connection(
            conn,
            lambda: database.get_messages_for_memory_extraction("thread-a", 41, 88),
        )

        self.assertEqual(rows[0]["created_at"], created)
        sql, args = conn.calls[0][1], conn.calls[0][2]
        self.assertIn("id > $2", sql)
        self.assertIn("id <= $3", sql)
        self.assertEqual(args, ("thread-a", 41, 88))


def _load_background_processor(interval=2):
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "process_memories_background"
    )
    namespace = {
        "json": json,
        "uuid": uuid,
        "MEMORY_EXTRACT_ENABLED": True,
        "MEMORY_EXTRACT_INTERVAL": interval,
        "should_defer_extraction": lambda calls: bool(calls),
        "ensure_memory_extraction_state": AsyncMock(),
        "persist_conversation_batch": AsyncMock(return_value={
            "inserted": 2,
            "rerolled": False,
        }),
        "record_memory_extraction_round": AsyncMock(),
        "get_recent_memories": AsyncMock(return_value=[]),
        "get_messages_for_memory_extraction": AsyncMock(return_value=[]),
        "extract_memories": AsyncMock(return_value=[]),
        "save_memory": AsyncMock(return_value=1),
        "link_memory_entities": AsyncMock(),
        "mark_memories_entity_scanned": AsyncMock(),
        "complete_memory_extraction": AsyncMock(return_value=True),
        "release_memory_extraction_claim": AsyncMock(return_value=True),
        "get_all_memories_count": AsyncMock(return_value=0),
        "_memory_extractor_module": types.SimpleNamespace(
            MEMORY_EXTRACTION_LAST_ERROR="test extraction failure",
        ),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace, namespace["process_memories_background"]


def _persistence_plan(completed_round=True):
    return type("Plan", (), {
        "session_id": "thread-a",
        "messages": (
            {"role": "user", "content": "user"},
            {"role": "assistant", "content": "assistant"},
        ),
        "completed_round": completed_round,
    })()


class BackgroundExtractionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_interval_persists_one_round_without_extracting(self):
        namespace, process = _load_background_processor(interval=20)
        namespace["record_memory_extraction_round"].return_value = {
            "should_extract": False,
            "pending_rounds": 1,
        }

        await process(
            "thread-a", "user", "assistant", "model",
            persistence_plan=_persistence_plan(),
        )

        namespace["ensure_memory_extraction_state"].assert_awaited_once_with("thread-a")
        namespace["record_memory_extraction_round"].assert_awaited_once()
        namespace["extract_memories"].assert_not_awaited()

    async def test_successful_empty_result_advances_claimed_batch(self):
        namespace, process = _load_background_processor(interval=2)
        claim = {
            "should_extract": True,
            "claim_token": "claim-a",
            "claimed_rounds": 2,
            "last_extracted_message_id": 10,
            "through_message_id": 14,
        }
        namespace["record_memory_extraction_round"].return_value = claim
        namespace["get_messages_for_memory_extraction"].return_value = [
            {"id": 11, "role": "user", "content": "user", "created_at": None},
            {"id": 12, "role": "assistant", "content": "assistant", "created_at": None},
        ]
        namespace["extract_memories"].return_value = []

        await process(
            "thread-a", "user", "assistant", "model",
            persistence_plan=_persistence_plan(),
        )

        namespace["complete_memory_extraction"].assert_awaited_once_with(
            "thread-a", "claim-a", 14, 2,
        )
        namespace["release_memory_extraction_claim"].assert_not_awaited()

    async def test_failed_extraction_releases_claim_without_advancing(self):
        namespace, process = _load_background_processor(interval=2)
        namespace["record_memory_extraction_round"].return_value = {
            "should_extract": True,
            "claim_token": "claim-a",
            "claimed_rounds": 2,
            "last_extracted_message_id": 10,
            "through_message_id": 14,
        }
        namespace["get_messages_for_memory_extraction"].return_value = [
            {"id": 11, "role": "user", "content": "user", "created_at": None},
        ]
        namespace["extract_memories"].return_value = None

        await process(
            "thread-a", "user", "assistant", "model",
            persistence_plan=_persistence_plan(),
        )

        namespace["release_memory_extraction_claim"].assert_awaited_once_with(
            "thread-a", "claim-a",
        )
        namespace["complete_memory_extraction"].assert_not_awaited()

    async def test_tool_call_intermediate_response_does_not_increment_round(self):
        namespace, process = _load_background_processor(interval=2)
        tool_calls = [{"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}]

        await process(
            "thread-a",
            "user",
            "",
            "model",
            assistant_tool_calls=tool_calls,
            persistence_plan=_persistence_plan(completed_round=False),
        )

        namespace["ensure_memory_extraction_state"].assert_not_awaited()
        namespace["persist_conversation_batch"].assert_not_awaited()
        namespace["record_memory_extraction_round"].assert_not_awaited()

    async def test_tool_chain_final_response_increments_exactly_once(self):
        namespace, process = _load_background_processor(interval=20)
        namespace["record_memory_extraction_round"].return_value = {
            "should_extract": False,
            "pending_rounds": 1,
        }

        await process(
            "thread-a",
            "original user message",
            "final answer",
            "model",
            persistence_plan=_persistence_plan(),
        )

        namespace["record_memory_extraction_round"].assert_awaited_once()
        namespace["persist_conversation_batch"].assert_awaited_once()

    async def test_reroll_does_not_increment_round(self):
        namespace, process = _load_background_processor(interval=20)
        namespace["persist_conversation_batch"].return_value = {
            "inserted": 0,
            "rerolled": True,
        }

        await process(
            "thread-a", "same prompt", "replacement answer", "model",
            persistence_plan=_persistence_plan(),
        )

        namespace["persist_conversation_batch"].assert_awaited_once()
        namespace["record_memory_extraction_round"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
