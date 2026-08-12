import unittest

import main


class PartitionToolRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_x = main.CACHE_PARTITION_X
        self.original_trigger = main.CACHE_PARTITION_TRIGGER
        self.original_summary_model = main.CACHE_SUMMARY_MODEL
        self.original_get_state = main.get_session_cache_state
        self.original_save_state = main.save_session_cache_state
        main.CACHE_PARTITION_X = 2
        main.CACHE_PARTITION_TRIGGER = "rounds"
        main.CACHE_SUMMARY_MODEL = ""
        self.saved_states = []

        async def get_state(_session_id):
            return {"summary_parts": [], "a_start_round": 0}

        async def save_state(*args):
            self.saved_states.append(args)

        main.get_session_cache_state = get_state
        main.save_session_cache_state = save_state

    async def asyncTearDown(self):
        main.CACHE_PARTITION_X = self.original_x
        main.CACHE_PARTITION_TRIGGER = self.original_trigger
        main.CACHE_SUMMARY_MODEL = self.original_summary_model
        main.get_session_cache_state = self.original_get_state
        main.save_session_cache_state = self.original_save_state

    async def test_rotation_defers_and_forwards_a_completed_tool_tail(self):
        history = [
            {"role": "user", "content": "a1"},
            {"role": "assistant", "content": "a1 answer"},
            {"role": "user", "content": "a2"},
            {"role": "assistant", "content": "a2 answer"},
            {"role": "user", "content": "b1"},
            {"role": "assistant", "content": "b1 answer"},
            {"role": "user", "content": "run tool"},
            {
                "role": "assistant",
                "content": "I will check that.",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ]

        forwarded = await main.build_partitioned_messages(
            "thread", history, "", "run tool"
        )

        tool_call = next(
            message for message in forwarded if message.get("tool_calls")
        )
        self.assertEqual(tool_call["tool_calls"][0]["id"], "call-1")
        self.assertIn(
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            forwarded,
        )
        self.assertEqual(self.saved_states, [])


if __name__ == "__main__":
    unittest.main()
