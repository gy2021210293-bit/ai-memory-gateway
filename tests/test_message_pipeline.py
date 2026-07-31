import unittest

from message_pipeline import (
    classify_request,
    combine_system_prompt,
    make_persistence_plan,
    reconcile_partition_block,
    validate_tool_sequence,
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

    def test_empty_current_block_never_falls_back_to_old_user(self):
        result = classify_request([
            {"role": "user", "content": "嗯嗯，可能是网关那边做了清洗"},
            {"role": "assistant", "content": "old answer"},
        ])

        self.assertEqual(result.current_block, ())
        self.assertEqual(result.latest_user_text, "")

    def test_aligns_full_client_history_and_uses_workflow_user(self):
        database = [
            {"role": "user", "content": "嗯嗯，可能是网关那边做了清洗"},
            {"role": "assistant", "content": "old answer"},
        ]
        client = [
            {"role": "system", "content": "client rules"},
            *database,
            {"role": "user", "content": "执行早安天气工作流"},
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "aligned")
        self.assertEqual(result.aligned_count, 2)
        self.assertEqual(result.latest_user_text, "执行早安天气工作流")
        self.assertEqual(
            [message["content"] for message in result.provider_messages],
            ["执行早安天气工作流"],
        )

    def test_aligns_truncated_client_history_against_database_suffix(self):
        database = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent answer"},
        ]
        client = [
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "workflow request"},
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "aligned")
        self.assertEqual(result.aligned_count, 1)
        self.assertEqual(result.latest_user_text, "workflow request")
        self.assertEqual(len(result.provider_messages), 1)

    def test_database_tool_prefix_allows_only_new_tool_result(self):
        database = [
            {"role": "user", "content": "run workflow"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
        ]
        client = [
            *database,
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "aligned")
        self.assertTrue(result.is_tool_chain)
        self.assertEqual(result.latest_user_text, "run workflow")
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["tool"],
        )
        self.assertEqual(result.persistence_messages, result.provider_messages)

    def test_dynamic_environment_and_system_do_not_participate_in_alignment(self):
        classified = classify_request([
            {"role": "system", "content": "client rules"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "user",
                "content": "battery=80",
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "workflow"},
        ])

        result = reconcile_partition_block(
            [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
            ],
            list(classified.ordinary_messages),
        )

        self.assertEqual(result.latest_user_text, "workflow")
        self.assertNotIn("battery=80", str(result.provider_messages))
        self.assertEqual(classified.client_system_prompts, ("client rules",))

    def test_invalid_environment_marker_does_not_drop_workflow_user(self):
        database = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
        ]
        classified = classify_request([
            *database,
            {
                "role": "user",
                "content": [{"type": "text", "text": "run workflow"}],
                "metadata": {"dynamic_environment": True},
            },
        ])

        self.assertEqual(classified.invalid_dynamic_count, 1)
        self.assertEqual(classified.latest_user_text, "run workflow")
        self.assertNotIn("metadata", classified.ordinary_messages[-1])

        result = reconcile_partition_block(
            database,
            list(classified.ordinary_messages),
        )

        self.assertEqual(result.reason, "aligned")
        self.assertEqual(result.latest_user_text, "run workflow")
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["user"],
        )

    def test_no_new_message_after_alignment_is_rejected(self):
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
        ]

        result = reconcile_partition_block(history, history)

        self.assertEqual(result.provider_messages, ())
        self.assertEqual(result.latest_user_text, "")
        self.assertEqual(result.reason, "no_new_client_messages")

    def test_mismatched_aligned_tool_chain_is_rejected(self):
        database = [{"role": "user", "content": "run"}]
        client = [
            *database,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "tool_call_id": "call-x", "content": "bad"},
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.provider_messages, ())
        self.assertEqual(result.reason, "invalid_aligned_message_sequence")

    def test_ambiguous_history_alignment_is_rejected(self):
        database = [{"role": "assistant", "content": "same"}]
        client = [
            {"role": "assistant", "content": "same"},
            {"role": "user", "content": "middle"},
            {"role": "assistant", "content": "same"},
            {"role": "user", "content": "workflow"},
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.provider_messages, ())
        self.assertEqual(result.reason, "ambiguous_history_alignment")

    def test_parallel_tool_results_may_arrive_in_any_order(self):
        result = validate_tool_sequence([
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1"},
                    {"id": "call-2"},
                    {"id": "call-3"},
                    {"id": "call-4"},
                ],
            },
            {"role": "tool", "tool_call_id": "call-3", "content": "three"},
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
            {"role": "tool", "tool_call_id": "call-4", "content": "four"},
            {"role": "tool", "tool_call_id": "call-2", "content": "two"},
            {"role": "assistant", "content": "done"},
        ])

        self.assertTrue(result.valid)

    def test_multiple_parallel_tool_steps_are_valid(self):
        result = validate_tool_sequence([
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}, {"id": "call-2"}],
            },
            {"role": "tool", "tool_call_id": "call-2", "content": "two"},
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-3"}],
            },
            {"role": "tool", "tool_call_id": "call-3", "content": "three"},
        ])

        self.assertTrue(result.valid)

    def test_consecutive_tool_call_assistants_are_rejected(self):
        result = validate_tool_sequence([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-2"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
        ])

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "unclosed_tool_calls")
        self.assertEqual(result.index, 1)

    def test_invalid_tool_ids_are_rejected(self):
        cases = [
            (
                [{"role": "tool", "tool_call_id": "call-1", "content": "one"}],
                "orphan_tool_result",
            ),
            (
                [
                    {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "content": "one"},
                ],
                "missing_tool_call_id",
            ),
            (
                [
                    {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-x", "content": "bad"},
                ],
                "unexpected_tool_call_id",
            ),
            (
                [
                    {
                        "role": "assistant",
                        "tool_calls": [{"id": "call-1"}, {"id": "call-1"}],
                    },
                ],
                "duplicate_tool_call_id",
            ),
            (
                [
                    {
                        "role": "assistant",
                        "tool_calls": [{"id": "call-1"}, {"id": "call-2"}],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "one"},
                    {"role": "tool", "tool_call_id": "call-1", "content": "again"},
                ],
                "duplicate_tool_result",
            ),
        ]

        for messages, reason in cases:
            with self.subTest(reason=reason):
                result = validate_tool_sequence(messages)
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, reason)


if __name__ == "__main__":
    unittest.main()
