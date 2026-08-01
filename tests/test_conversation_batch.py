import ast
import json
from pathlib import Path
import unittest
from message_pipeline import normalize_content_text


def _load_batch_functions():
    source = Path("database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_conversation_message_parts",
        "_conversation_signature",
        "persist_conversation_batch",
    }
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    namespace = {
        "json": json,
        "normalize_content_text": normalize_content_text,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "database.py", "exec"), namespace)
    return namespace


def _load_history_function():
    source = Path("database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "get_conversation_messages"
    ]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "database.py", "exec"), namespace)
    return namespace


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []
        self.fetch_queries = []

    def transaction(self):
        return _AsyncContext()

    async def fetch(self, query, *args):
        self.fetch_queries.append(query)
        return self.rows

    async def execute(self, query, *args):
        self.executions.append((query, args))


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


class ConversationBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_content_is_normalized_before_database_write(self):
        namespace = _load_batch_functions()
        connection = _Connection([])
        incoming = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            },
            {
                "role": "tool",
                "content": [{"type": "image_url", "image_url": {"url": "redacted"}}],
                "tool_call_id": "call-1",
            },
        ]

        async def get_pool():
            return _Pool(connection)

        namespace["get_pool"] = get_pool
        await namespace["persist_conversation_batch"]("thread-a", incoming, "model-a")

        inserts = [
            args
            for query, args in connection.executions
            if "INSERT INTO conversations" in query
        ]
        self.assertEqual([args[2] for args in inserts], ["first\nsecond", "[图片]"])
        self.assertTrue(all(isinstance(args[2], str) for args in inserts))

    async def test_history_uses_id_as_the_stable_message_order(self):
        namespace = _load_history_function()
        connection = _Connection([
            {"role": "assistant", "content": "", "metadata": None, "created_at": "same"},
            {"role": "tool", "content": "result", "metadata": None, "created_at": "same"},
        ])

        async def get_pool():
            return _Pool(connection)

        namespace["get_pool"] = get_pool
        rows = await namespace["get_conversation_messages"]("thread-a", 100)

        self.assertEqual(len(rows), 2)
        query = connection.fetch_queries[0]
        self.assertIn("ORDER BY id ASC", query)

    async def test_closed_tool_chain_only_inserts_missing_suffix(self):
        namespace = _load_batch_functions()
        rows = [
            {
                "role": "assistant",
                "content": "",
                "metadata": '{"tool_calls": [{"id": "call-1"}]}',
            },
            {"role": "user", "content": "run", "metadata": None},
        ]
        connection = _Connection(rows)
        incoming = [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "content": "result", "tool_call_id": "call-1"},
            {"role": "assistant", "content": "done"},
        ]

        async def get_pool():
            return _Pool(connection)

        namespace["get_pool"] = get_pool
        result = await namespace["persist_conversation_batch"](
            "thread-a", incoming, "model-a"
        )

        self.assertEqual(result, {"inserted": 2, "rerolled": False})
        sql = "\n".join(query for query, _ in connection.executions)
        self.assertIn("pg_advisory_xact_lock", sql)
        inserts = [
            args
            for query, args in connection.executions
            if "INSERT INTO conversations" in query
        ]
        self.assertEqual([args[1] for args in inserts], ["tool", "assistant"])

    async def test_reroll_updates_last_assistant_without_inserting(self):
        namespace = _load_batch_functions()
        rows = [
            {"role": "assistant", "content": "old answer", "metadata": None},
            {"role": "user", "content": "same request", "metadata": None},
        ]
        connection = _Connection(rows)

        async def get_pool():
            return _Pool(connection)

        namespace["get_pool"] = get_pool
        result = await namespace["persist_conversation_batch"](
            "thread-a",
            [
                {"role": "user", "content": "same request"},
                {"role": "assistant", "content": "new answer"},
            ],
            "model-a",
        )

        self.assertEqual(result, {"inserted": 0, "rerolled": True})
        self.assertTrue(any(
            "UPDATE conversations" in query
            for query, _ in connection.executions
        ))

    async def test_repeated_complete_batch_inserts_nothing(self):
        namespace = _load_batch_functions()
        rows = [
            {"role": "assistant", "content": "done", "metadata": None},
            {
                "role": "tool",
                "content": "result",
                "metadata": '{"tool_call_id": "call-1"}',
            },
            {
                "role": "assistant",
                "content": "",
                "metadata": '{"tool_calls": [{"id": "call-1"}]}',
            },
            {"role": "user", "content": "run", "metadata": None},
        ]
        connection = _Connection(rows)

        async def get_pool():
            return _Pool(connection)

        namespace["get_pool"] = get_pool
        result = await namespace["persist_conversation_batch"](
            "thread-a",
            [
                {"role": "user", "content": "run"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-1"}],
                },
                {"role": "tool", "content": "result", "tool_call_id": "call-1"},
                {"role": "assistant", "content": "done"},
            ],
            "model-a",
        )

        self.assertEqual(result, {"inserted": 0, "rerolled": False})
        self.assertFalse(any(
            "INSERT INTO conversations" in query
            for query, _ in connection.executions
        ))


if __name__ == "__main__":
    unittest.main()
