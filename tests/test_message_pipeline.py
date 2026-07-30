import unittest

from message_pipeline import (
    classify_request,
    combine_system_prompt,
    make_persistence_plan,
)


class MessagePipelineTests(unittest.TestCase):
    def test_classification_does_not_modify_input_and_isolates_environment(self):
        messages = [
            {"role": "system", "content": "client rules"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
            {
                "role": "user",
                "content": "battery=80",
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "current"},
        ]
        original = [dict(message) for message in messages]

        result = classify_request(messages)

        self.assertEqual(messages, original)
        self.assertEqual(result.dynamic_environment, "battery=80")
        self.assertEqual(result.latest_user_text, "current")
        self.assertEqual(
            [message["content"] for message in result.current_block],
            ["current"],
        )
        self.assertNotIn("battery=80", str(result.ordinary_messages))

    def test_closed_multi_step_tool_chain_is_the_current_block(self):
        messages = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "run workflow"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-2"}],
            },
            {"role": "tool", "tool_call_id": "call-2", "content": "two"},
        ]

        result = classify_request(messages)

        self.assertTrue(result.is_tool_chain)
        self.assertEqual(len(result.current_block), 5)
        self.assertEqual(result.current_block[0]["content"], "run workflow")

    def test_mismatched_tool_result_is_rejected(self):
        result = classify_request([
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "tool_call_id": "call-x", "content": "bad"},
        ])

        self.assertEqual(result.current_block, ())
        self.assertFalse(result.is_tool_chain)

    def test_system_order_and_persistence_boundary(self):
        system = combine_system_prompt("gateway", ("client one", "client two"))
        plan = make_persistence_plan(
            "thread-a",
            ({"role": "user", "content": "request"},),
            "answer",
            None,
            None,
            False,
        )

        self.assertEqual(system, "gateway\n\nclient one\n\nclient two")
        self.assertEqual(
            [message["role"] for message in plan.messages],
            ["user", "assistant"],
        )
        self.assertTrue(plan.completed_round)
        self.assertNotIn("gateway", str(plan.messages))

    def test_tool_request_is_not_a_completed_round(self):
        plan = make_persistence_plan(
            "thread-a",
            ({"role": "user", "content": "request"},),
            "",
            [{"id": "call-1"}],
            None,
            False,
        )

        self.assertFalse(plan.completed_round)
        self.assertEqual(plan.messages[-1]["tool_calls"][0]["id"], "call-1")


if __name__ == "__main__":
    unittest.main()
