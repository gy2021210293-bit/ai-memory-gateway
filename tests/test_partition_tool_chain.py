import ast
import unittest
from pathlib import Path


def _load_helper():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_extract_trailing_client_block"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"),
        namespace,
    )
    return namespace["_extract_trailing_client_block"]


def assistant_call(*call_ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function"} for call_id in call_ids],
    }


def tool_result(call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": f"{call_id}-result"}


class PartitionToolChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extract = staticmethod(_load_helper())

    def test_keeps_trailing_user_messages(self):
        messages = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "image"},
            {"role": "user", "content": "current"},
        ]

        block, is_tool_chain = self.extract(messages)

        self.assertFalse(is_tool_chain)
        self.assertEqual([message["content"] for message in block], ["image", "current"])

    def test_keeps_closed_user_assistant_tool_chain(self):
        messages = [
            {"role": "system", "content": "workflow"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "run workflow"},
            assistant_call("call-a", "call-b"),
            tool_result("call-a"),
            tool_result("call-b"),
        ]

        block, is_tool_chain = self.extract(messages)

        self.assertTrue(is_tool_chain)
        self.assertEqual(
            [message["role"] for message in block],
            ["user", "assistant", "tool", "tool"],
        )
        self.assertEqual(block[0]["content"], "run workflow")

    def test_keeps_multiple_closed_tool_steps_after_one_user(self):
        messages = [
            {"role": "user", "content": "run workflow"},
            assistant_call("call-a"),
            tool_result("call-a"),
            assistant_call("call-b"),
            tool_result("call-b"),
        ]

        block, is_tool_chain = self.extract(messages)

        self.assertTrue(is_tool_chain)
        self.assertEqual(len(block), 5)
        self.assertEqual(block[0]["role"], "user")
        self.assertEqual(block[-1]["tool_call_id"], "call-b")

    def test_rejects_mismatched_tool_ids(self):
        messages = [
            {"role": "user", "content": "run workflow"},
            assistant_call("call-a"),
            tool_result("call-b"),
        ]

        block, is_tool_chain = self.extract(messages)

        self.assertFalse(is_tool_chain)
        self.assertEqual(block, [])

    def test_rejects_orphan_tools(self):
        block, is_tool_chain = self.extract([
            {"role": "user", "content": "old"},
            tool_result("call-a"),
        ])

        self.assertFalse(is_tool_chain)
        self.assertEqual(block, [])

    def test_removes_internal_metadata_before_forwarding(self):
        block, _ = self.extract([{
            "role": "user",
            "content": "current",
            "metadata": {"workflow_request": True},
        }])

        self.assertNotIn("metadata", block[0])


if __name__ == "__main__":
    unittest.main()
