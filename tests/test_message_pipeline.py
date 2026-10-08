import unittest
from datetime import datetime, timedelta, timezone

from message_pipeline import (
    classify_request,
    combine_system_prompt,
    extract_current_block,
    has_closed_tool_tail,
    leading_user_messages,
    make_persistence_plan,
    reconcile_partition_block,
    validate_tool_sequence,
)


def _fresh_generated_at():
    return datetime.now(timezone.utc).isoformat()


class MessagePipelineTests(unittest.TestCase):
    def test_classification_does_not_modify_input_and_isolates_environment(self):
        messages = [
            {"role": "system", "content": "client rules"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
            {
                "role": "user",
                "content": "battery=80",
                "metadata": {
                    "dynamic_environment": True,
                    "generated_at": _fresh_generated_at(),
                },
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

    def test_final_answer_persists_the_entire_tool_workflow_as_one_round(self):
        plan = make_persistence_plan(
            "thread-a",
            (
                {"role": "user", "content": "request"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-2"}]},
                {"role": "tool", "tool_call_id": "call-2", "content": "result"},
            ),
            "final answer",
            None,
            None,
            False,
        )

        self.assertTrue(plan.completed_round)
        self.assertEqual(
            [message["role"] for message in plan.messages],
            ["user", "assistant", "tool", "assistant", "tool", "assistant"],
        )

    def test_closed_tool_tail_is_detected_for_rotation_deferral(self):
        messages = [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "calling tools",
                "tool_calls": [{"id": "call-a"}, {"id": "call-b"}],
            },
            {"role": "tool", "tool_call_id": "call-b", "content": "two"},
            {"role": "tool", "tool_call_id": "call-a", "content": "one"},
        ]

        self.assertTrue(has_closed_tool_tail(messages))

    def test_incomplete_or_duplicate_tool_tail_does_not_defer_rotation(self):
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-a"}, {"id": "call-b"}],
        }
        self.assertFalse(has_closed_tool_tail([
            {"role": "user", "content": "run"},
            assistant,
            {"role": "tool", "tool_call_id": "call-a", "content": "one"},
        ]))
        self.assertFalse(has_closed_tool_tail([
            {"role": "user", "content": "run"},
            assistant,
            {"role": "tool", "tool_call_id": "call-a", "content": "one"},
            {"role": "tool", "tool_call_id": "call-a", "content": "again"},
        ]))

    def test_upstream_rejection_plan_keeps_user_without_completing_round(self):
        plan = make_persistence_plan(
            "thread-a",
            ({"role": "user", "content": [{"type": "text", "text": "request"}]},),
            "",
            None,
            None,
            False,
        )

        self.assertEqual(len(plan.messages), 1)
        self.assertEqual(plan.messages[0]["role"], "user")
        self.assertFalse(plan.completed_round)

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
                "metadata": {
                    "dynamic_environment": True,
                    "generated_at": _fresh_generated_at(),
                },
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

    def test_text_list_environment_is_valid_and_isolated(self):
        database = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
        ]
        classified = classify_request([
            *database,
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<dynamic_context generated_at=\"t1\">battery=80\ncharging</dynamic_context>",
                    },
                ],
                "metadata": {
                    "dynamic_environment": True,
                    "generated_at": _fresh_generated_at(),
                },
            },
            {"role": "user", "content": "run workflow"},
        ])

        self.assertEqual(classified.invalid_dynamic_count, 0)
        self.assertIn("battery=80", classified.dynamic_environment)
        self.assertEqual(classified.latest_user_text, "run workflow")
        self.assertNotIn("battery=80", str(classified.ordinary_messages))

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

    def test_mislabeled_text_list_workflow_is_preserved(self):
        classified = classify_request([
            {
                "role": "user",
                "content": [{"type": "text", "text": "run workflow"}],
                "metadata": {"dynamic_environment": True, "generated_at": "t1"},
            },
        ])

        self.assertEqual(classified.invalid_dynamic_count, 1)
        self.assertEqual(classified.dynamic_environment, "")
        self.assertEqual(classified.latest_user_text, "run workflow")
        self.assertEqual(len(classified.current_block), 1)
        self.assertNotIn("metadata", classified.current_block[0])

    def test_mixed_environment_marker_does_not_drop_original_message(self):
        classified = classify_request([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "redacted"}},
                ],
                "metadata": {"dynamic_environment": True},
            },
        ])

        self.assertEqual(classified.invalid_dynamic_count, 1)
        self.assertEqual(classified.dynamic_environment, "")
        self.assertEqual(classified.latest_user_text, "look")
        self.assertNotIn("metadata", classified.ordinary_messages[0])

    def test_historical_snapshot_is_not_reused_for_new_user_turn(self):
        classified = classify_request([
            {
                "role": "user",
                "content": "<dynamic_context>phone=true</dynamic_context>",
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "old mobile request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new desktop request"},
        ])

        self.assertEqual(classified.dynamic_environment, "")
        self.assertEqual(classified.latest_user_text, "new desktop request")

    def test_expired_current_snapshot_is_discarded(self):
        generated_at = (
            datetime.now(timezone.utc) - timedelta(minutes=11)
        ).isoformat()
        classified = classify_request([
            {
                "role": "user",
                "content": "<dynamic_context>phone=true</dynamic_context>",
                "metadata": {
                    "dynamic_environment": True,
                    "generated_at": generated_at,
                },
            },
            {"role": "user", "content": "new desktop request"},
        ])

        self.assertEqual(classified.dynamic_environment, "")
        self.assertEqual(classified.stale_dynamic_count, 1)

    def test_current_snapshot_without_timestamp_is_discarded(self):
        classified = classify_request([
            {
                "role": "user",
                "content": "<dynamic_context>phone=true</dynamic_context>",
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "new desktop request"},
        ])

        self.assertEqual(classified.dynamic_environment, "")
        self.assertEqual(classified.stale_dynamic_count, 1)

    def test_historical_snapshot_does_not_change_current_tool_chain(self):
        classified = classify_request([
            {
                "role": "user",
                "content": "<dynamic_context>phone=true</dynamic_context>",
                "metadata": {"dynamic_environment": True},
            },
            {"role": "user", "content": "old mobile request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "run tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "content": "result", "tool_call_id": "call-1"},
        ])

        self.assertEqual(classified.dynamic_environment, "")
        self.assertTrue(classified.is_tool_chain)
        self.assertEqual(
            [message["role"] for message in classified.current_block],
            ["user", "assistant", "tool"],
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


def _call(*call_ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function"} for call_id in call_ids],
    }


def _result(call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": f"{call_id}-result"}


class DbAssistantSuppliedTests(unittest.TestCase):
    """客户端回传工具结果时丢掉 assistant(tool_calls) 消息 → 由 DB 补全调用方。"""

    def test_db_assistant_supplied_when_client_drops_assistant(self):
        database = [
            {"role": "user", "content": "run workflow"},
            _call("call-1"),
        ]
        client = [
            {"role": "user", "content": "run workflow"},
            _result("call-1"),
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "db_assistant_supplied")
        self.assertTrue(result.is_tool_chain)
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["tool"],
        )
        self.assertEqual(result.latest_user_text, "run workflow")
        self.assertTrue(validate_tool_sequence(database + list(result.provider_messages)).valid)

    def test_db_assistant_supplied_with_parallel_results_any_order(self):
        database = [
            {"role": "user", "content": "run"},
            _call("call-a", "call-b"),
        ]
        client = [
            {"role": "user", "content": "run"},
            _result("call-b"),
            _result("call-a"),
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "db_assistant_supplied")
        self.assertEqual(len(result.provider_messages), 2)
        self.assertTrue(result.is_tool_chain)

    def test_db_assistant_supplied_without_any_client_history(self):
        database = [
            {"role": "user", "content": "run"},
            _call("call-1"),
        ]
        client = [_result("call-1")]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "db_assistant_supplied")
        self.assertEqual(result.latest_user_text, "run")
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["tool"],
        )

    def test_no_supply_when_tool_ids_mismatch_db(self):
        database = [
            {"role": "user", "content": "run"},
            _call("call-1"),
        ]
        client = [
            {"role": "user", "content": "run"},
            _result("call-2"),
        ]

        result = reconcile_partition_block(database, client)

        # 无法归属任何调用方 → 维持原有拒绝
        self.assertEqual(result.reason, "no_valid_current_block")
        self.assertEqual(result.provider_messages, ())

    def test_no_supply_when_db_tail_is_not_the_caller(self):
        database = [
            {"role": "user", "content": "run"},
            _call("call-1"),
            _result("call-1"),
        ]
        client = [
            {"role": "user", "content": "run"},
            _result("call-1"),
        ]

        result = reconcile_partition_block(database, client)

        # DB 尾部不是 assistant（链已闭合在 DB 内）→ 不合成，维持原有行为
        self.assertNotEqual(result.reason, "db_assistant_supplied")

    def test_normal_flow_prefers_alignment(self):
        database = [
            {"role": "user", "content": "run"},
            _call("call-1"),
        ]
        client = [
            {"role": "user", "content": "run"},
            _call("call-1"),
            _result("call-1"),
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.reason, "aligned")
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["tool"],
        )


class PersistenceRoundTests(unittest.TestCase):
    """触发工具链的用户消息必须在整轮最终入库时保留在批次开头。"""

    def test_user_leading_detects_a_user_started_round(self):
        plan = make_persistence_plan(
            "thread-a",
            ({"role": "user", "content": "run tool"},),
            "",
            [{"id": "call-1"}],
            None,
            False,
        )
        self.assertTrue(plan.user_leading)
        self.assertEqual(
            leading_user_messages(plan.messages),
            ({"role": "user", "content": "run tool"},),
        )

    def test_tool_delta_has_no_user_leading(self):
        plan = make_persistence_plan(
            "thread-a",
            (
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            ),
            "final answer",
            None,
            None,
            False,
        )
        self.assertFalse(plan.user_leading)
        self.assertEqual(leading_user_messages(plan.messages), ())

    def test_leading_user_messages_stops_at_first_non_user(self):
        messages = (
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "answer"},
        )
        self.assertEqual(
            [m["content"] for m in leading_user_messages(messages)],
            ["one", "two"],
        )

    def test_make_persistence_plan_prepends_stashed_trigger_user(self):
        plan = make_persistence_plan(
            "thread-a",
            (
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            ),
            "final answer",
            None,
            None,
            False,
            leading_user_messages=({"role": "user", "content": "run tool"},),
        )
        self.assertTrue(plan.completed_round)
        self.assertEqual(
            [message["role"] for message in plan.messages],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(plan.messages[0]["content"], "run tool")


class SelfContainedToolChainTests(unittest.TestCase):
    """客户端以增量回传 [assistant(tool_calls), tool]（无前导 user）时的当前块识别。"""

    def test_extract_self_contained_tool_chain(self):
        block, is_tool_chain = extract_current_block([
            _call("call-1"),
            _result("call-1"),
        ])

        self.assertTrue(is_tool_chain)
        self.assertEqual(
            [message["role"] for message in block],
            ["assistant", "tool"],
        )

    def test_extract_multi_step_self_contained_tool_chain(self):
        block, is_tool_chain = extract_current_block([
            _call("call-1"),
            _result("call-1"),
            _call("call-2"),
            _result("call-2"),
        ])

        self.assertTrue(is_tool_chain)
        self.assertEqual(
            [message["role"] for message in block],
            ["assistant", "tool", "assistant", "tool"],
        )

    def test_mismatched_self_contained_tool_chain_is_still_rejected(self):
        block, is_tool_chain = extract_current_block([
            _call("call-1"),
            _result("call-x"),
        ])

        self.assertEqual(block, [])
        self.assertFalse(is_tool_chain)

    def test_reconcile_delta_tool_chain_without_user(self):
        database = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older answer"},
        ]
        client = [
            _call("call-1"),
            _result("call-1"),
        ]

        result = reconcile_partition_block(database, client)

        self.assertNotEqual(result.reason, "no_valid_current_block")
        self.assertTrue(result.is_tool_chain)
        self.assertEqual(
            [message["role"] for message in result.provider_messages],
            ["assistant", "tool"],
        )
        self.assertTrue(
            validate_tool_sequence(database + list(result.provider_messages)).valid
        )

    def test_mismatched_delta_tool_chain_is_still_rejected(self):
        database = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older answer"},
        ]
        client = [
            _call("call-1"),
            _result("call-x"),
        ]

        result = reconcile_partition_block(database, client)

        self.assertEqual(result.provider_messages, ())
        self.assertEqual(result.reason, "no_valid_current_block")


if __name__ == "__main__":
    unittest.main()
