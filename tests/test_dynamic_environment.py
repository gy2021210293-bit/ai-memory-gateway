import ast
import unittest
from pathlib import Path
from message_pipeline import classify_request


def _load_helpers():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "_assemble_current_user_message",
        "_inject_dynamic_environment",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {}
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"),
        namespace,
    )
    namespace["_extract_dynamic_environment"] = lambda messages: (
        list(classify_request(messages).ordinary_messages),
        classify_request(messages).dynamic_environment,
    )
    return namespace


class DynamicEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_helpers()

    def test_extracts_last_valid_snapshot_and_removes_all_marked_messages(self):
        messages = [
            {"role": "system", "content": "stable"},
            {
                "role": "user",
                "content": "battery=50",
                "metadata": {"dynamic_environment": True, "generated_at": "t1"},
            },
            {
                "role": "user",
                "content": "battery=51",
                "metadata": {"dynamic_environment": True, "generated_at": "t1"},
            },
            {"role": "user", "content": "real request"},
        ]

        filtered, snapshot = self.helpers["_extract_dynamic_environment"](messages)

        self.assertEqual(snapshot, "battery=51")
        self.assertEqual(
            filtered,
            [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "real request"},
            ],
        )

    def test_unmarked_adjacent_users_are_not_treated_as_snapshots(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]

        filtered, snapshot = self.helpers["_extract_dynamic_environment"](messages)

        self.assertEqual(filtered, messages)
        self.assertEqual(snapshot, "")

    def test_invalid_marked_message_is_removed_without_becoming_snapshot(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "invalid"}],
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "real request"},
        ]

        filtered, snapshot = self.helpers["_extract_dynamic_environment"](messages)

        self.assertEqual(filtered, [{"role": "user", "content": "real request"}])
        self.assertEqual(snapshot, "")

    def test_inserts_snapshot_before_latest_user_even_when_tool_is_last(self):
        messages = [
            {"role": "user", "content": "real request"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "call-1"},
        ]

        inserted = self.helpers["_inject_dynamic_environment"](
            messages,
            "battery=51",
        )

        self.assertTrue(inserted)
        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "user", "assistant", "tool"],
        )
        self.assertEqual(messages[0]["content"], "battery=51")
        self.assertEqual(messages[1]["content"], "real request")

    def test_anthropic_merge_keeps_snapshot_before_real_request(self):
        messages = [{"role": "user", "content": "real request"}]

        inserted = self.helpers["_inject_dynamic_environment"](
            messages,
            "battery=51",
            merge_with_user=True,
        )

        self.assertTrue(inserted)
        self.assertEqual(messages, [{
            "role": "user",
            "content": "battery=51\n\nreal request",
        }])


if __name__ == "__main__":
    unittest.main()
