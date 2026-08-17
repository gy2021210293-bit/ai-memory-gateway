import unittest
from unittest.mock import AsyncMock

import main


class _FakeResponse:
    def __init__(self, chunks):
        self.chunks = chunks
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        pass


class _FakeAsyncClient:
    response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, _request, stream=False):
        return self.response

    async def post(self, *_args, **_kwargs):
        return self.response


class _FakeJSONResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            }}],
        }


class _FakeRequest:
    headers = {}

    async def json(self):
        return {
            "model": "model",
            "stream": False,
            "messages": [{"role": "user", "content": "run tool"}],
        }


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

    async def test_stream_forwards_tool_call_sse_without_persisting(self):
        original_client = main.httpx.AsyncClient
        original_memory_enabled = main.MEMORY_ENABLED
        original_background = main.process_memories_background
        original_commit = main.commit_response_state

        _FakeAsyncClient.response = _FakeResponse([
            b'data: {"choices":[{"delta":{"content":"checking"}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[',
            b'{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            b'data: [DONE]\n\n',
        ])
        main.httpx.AsyncClient = _FakeAsyncClient
        main.MEMORY_ENABLED = True
        main.process_memories_background = AsyncMock()
        main.commit_response_state = AsyncMock(return_value=(None, None))
        try:
            stream = main.stream_and_capture(
                {}, {"stream": True}, "session", "run", "model",
                current_block=({"role": "user", "content": "run"},),
            )
            self.assertEqual(await anext(stream), b": keep-alive\n\n")
            self.assertIn(b'"content":"checking"', await anext(stream))
            tool_call_chunk = await anext(stream)
            self.assertIn(b'"tool_calls"', tool_call_chunk)
            self.assertIn(b'"finish_reason":"tool_calls"', await anext(stream))
            self.assertEqual(await anext(stream), b"data: [DONE]\n\n")
            main.process_memories_background.assert_not_called()
            main.commit_response_state.assert_awaited_once()
            await stream.aclose()
        finally:
            main.httpx.AsyncClient = original_client
            main.MEMORY_ENABLED = original_memory_enabled
            main.process_memories_background = original_background
            main.commit_response_state = original_commit

    async def test_non_stream_tool_call_does_not_schedule_persistence(self):
        original_client = main.httpx.AsyncClient
        original_memory_enabled = main.MEMORY_ENABLED
        original_extract_enabled = main.MEMORY_EXTRACT_ENABLED
        original_partition_enabled = main.CACHE_PARTITION_ENABLED
        original_force_stream = main.FORCE_STREAM
        original_session_id = main.get_active_session_id
        original_system_prompt = main.get_system_prompt
        original_background = main.process_memories_background
        original_commit = main.commit_response_state
        original_drives_enabled = main.drives.is_enabled

        async def system_prompt():
            return ""

        _FakeAsyncClient.response = _FakeJSONResponse()
        main.httpx.AsyncClient = _FakeAsyncClient
        main.MEMORY_ENABLED = True
        main.MEMORY_EXTRACT_ENABLED = False
        main.CACHE_PARTITION_ENABLED = False
        main.FORCE_STREAM = False
        main.get_active_session_id = lambda: "session"
        main.get_system_prompt = system_prompt
        main.process_memories_background = AsyncMock()
        main.commit_response_state = AsyncMock(return_value=(None, None))
        main.drives.is_enabled = lambda: False
        try:
            response = await main._chat_completions_inner(_FakeRequest())
            self.assertEqual(response.status_code, 200)
            main.process_memories_background.assert_not_called()
            main.commit_response_state.assert_awaited_once()
        finally:
            main.httpx.AsyncClient = original_client
            main.MEMORY_ENABLED = original_memory_enabled
            main.MEMORY_EXTRACT_ENABLED = original_extract_enabled
            main.CACHE_PARTITION_ENABLED = original_partition_enabled
            main.FORCE_STREAM = original_force_stream
            main.get_active_session_id = original_session_id
            main.get_system_prompt = original_system_prompt
            main.process_memories_background = original_background
            main.commit_response_state = original_commit
            main.drives.is_enabled = original_drives_enabled

    async def test_stream_converts_malformed_sse_to_a_safe_error(self):
        original_client = main.httpx.AsyncClient
        _FakeAsyncClient.response = _FakeResponse([
            b'data: {"id":"fa1553ac-f089-46ae-a80f-506848cc0e87","data: {"choices":[],"cost":"0"}\n\n',
        ])
        main.httpx.AsyncClient = _FakeAsyncClient
        try:
            stream = main.stream_and_capture({}, {"stream": True}, "session", "", "model")
            self.assertEqual(await anext(stream), b": keep-alive\n\n")
            error = await anext(stream)
            self.assertIn(b'"type": "upstream_malformed_sse"', error)
            self.assertIn(b"data: [DONE]", error)
            await stream.aclose()
        finally:
            main.httpx.AsyncClient = original_client

    async def test_stream_ignores_empty_choices_statistic_event(self):
        original_client = main.httpx.AsyncClient
        _FakeAsyncClient.response = _FakeResponse([
            b'data: {"id":"stats","choices":[],"cost":"0"}\n\n',
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b'data: [DONE]\n\n',
        ])
        main.httpx.AsyncClient = _FakeAsyncClient
        try:
            stream = main.stream_and_capture({}, {"stream": True}, "session", "", "model")
            self.assertEqual(await anext(stream), b": keep-alive\n\n")
            self.assertIn(b'"choices":[]', await anext(stream))
            self.assertIn(b'"content":"ok"', await anext(stream))
            self.assertEqual(await anext(stream), b"data: [DONE]\n\n")
            await stream.aclose()
        finally:
            main.httpx.AsyncClient = original_client


class PendingToolWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_start_is_staged_durably(self):
        original_get = main.get_pending_tool_workflow
        original_stage = main.stage_tool_workflow
        main.get_pending_tool_workflow = AsyncMock(return_value=None)
        main.stage_tool_workflow = AsyncMock(return_value={"status": "awaiting_tool_results"})
        try:
            plan, result = await main.commit_response_state(
                "thread-a", ({"role": "user", "content": "run tool"},),
                "", [{"id": "call-1"}], None, "model", False,
            )
            self.assertIsNone(plan)
            self.assertIsNone(result)
            staged = main.stage_tool_workflow.await_args.args[2]
            self.assertEqual([message["role"] for message in staged], ["user", "assistant"])
        finally:
            main.get_pending_tool_workflow = original_get
            main.stage_tool_workflow = original_stage

    async def test_final_tool_answer_commits_staged_workflow(self):
        original_get = main.get_pending_tool_workflow
        original_persist = main.persist_conversation_batch
        main.get_pending_tool_workflow = AsyncMock(return_value={
            "workflow_id": "wf-1",
            "messages": [
                {"role": "user", "content": "run tool"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            ],
        })
        main.persist_conversation_batch = AsyncMock(return_value={"inserted": 4, "rerolled": False})
        try:
            plan, result = await main.commit_response_state(
                "thread-a", ({"role": "tool", "tool_call_id": "call-1", "content": "result"},),
                "final answer", None, None, "model", False,
            )
            self.assertEqual(
                [message["role"] for message in plan.messages],
                ["user", "assistant", "tool", "assistant"],
            )
            self.assertEqual(result["inserted"], 4)
            self.assertEqual(main.persist_conversation_batch.await_args.kwargs["workflow_id"], "wf-1")
        finally:
            main.get_pending_tool_workflow = original_get
            main.persist_conversation_batch = original_persist


if __name__ == "__main__":
    unittest.main()
