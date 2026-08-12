import unittest

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

    async def test_stream_holds_tool_call_sse_until_persisted(self):
        original_client = main.httpx.AsyncClient
        original_memory_enabled = main.MEMORY_ENABLED
        original_persist = main.persist_tool_request_before_followup
        persisted = []

        async def persist_before_followup(plan, model):
            persisted.append((plan, model))
            return True

        _FakeAsyncClient.response = _FakeResponse([
            b'data: {"choices":[{"delta":{"content":"checking"}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[',
            b'{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            b'data: [DONE]\n\n',
        ])
        main.httpx.AsyncClient = _FakeAsyncClient
        main.MEMORY_ENABLED = True
        main.persist_tool_request_before_followup = persist_before_followup
        try:
            stream = main.stream_and_capture(
                {}, {"stream": True}, "session", "run", "model",
                current_block=({"role": "user", "content": "run"},),
            )
            self.assertEqual(await anext(stream), b": keep-alive\n\n")
            self.assertIn(b'"content":"checking"', await anext(stream))
            tool_call_chunk = await anext(stream)
            self.assertTrue(persisted)
            self.assertIn(b'"tool_calls"', tool_call_chunk)
            await stream.aclose()
        finally:
            main.httpx.AsyncClient = original_client
            main.MEMORY_ENABLED = original_memory_enabled
            main.persist_tool_request_before_followup = original_persist

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


if __name__ == "__main__":
    unittest.main()
