import unittest
from unittest.mock import AsyncMock, Mock, patch

import memory_extractor


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MemoryExtractorTests(unittest.IsolatedAsyncioTestCase):
    def test_fragment_prompt_keeps_existing_storage_contract(self):
        prompt = memory_extractor.EXTRACTION_PROMPT

        self.assertIn("事件和互动类碎片必须自然写出我当时有对话证据的具体情绪或感受", prompt)
        self.assertIn("客观信息类碎片可以不带情绪", prompt)
        self.assertIn("生日、职业、账号信息、航班号", prompt)
        self.assertIn("不要为了满足情绪要求编造感受", prompt)
        self.assertNotIn("宽泛主题", prompt)
        self.assertNotIn("主题不影响是否提取", prompt)
        self.assertNotIn("同一主题若含", prompt)
        self.assertIn("字符敏感信息必须逐字原样保留", prompt)
        self.assertIn("Moonlit0630!", prompt)
        self.assertIn("# 简短示例", prompt)
        self.assertIn("一条碎片只记一件事", prompt)
        self.assertIn("8-10（high）", prompt)
        self.assertIn('"content": "我以第一人称记住的内容"', prompt)
        self.assertNotIn('"mood":', prompt)
        self.assertNotIn('"topics":', prompt)

    def test_tool_call_response_defers_extraction_until_final_answer(self):
        tool_calls = [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }]

        self.assertTrue(memory_extractor.should_defer_extraction(tool_calls))
        self.assertFalse(memory_extractor.should_defer_extraction(None))
        self.assertFalse(memory_extractor.should_defer_extraction([]))

    def test_apply_runtime_config_updates_memory_provider_and_fallback(self):
        original = {
            key: getattr(memory_extractor, key)
            for key in (
                "API_KEY",
                "API_BASE_URL",
                "MEMORY_API_KEY",
                "MEMORY_API_BASE_URL",
                "MEMORY_MODEL",
            )
        }
        try:
            memory_extractor.apply_runtime_config("API_KEY", "main-key")
            memory_extractor.apply_runtime_config("API_BASE_URL", "https://main.example/chat")
            memory_extractor.apply_runtime_config("MEMORY_API_KEY", "")
            memory_extractor.apply_runtime_config("MEMORY_API_BASE_URL", "")
            memory_extractor.apply_runtime_config("MEMORY_MODEL", "memory-model")

            self.assertEqual(memory_extractor.get_memory_api_key(), "main-key")
            self.assertEqual(memory_extractor.get_memory_api_base_url(), "https://main.example/chat")
            self.assertEqual(memory_extractor.MEMORY_MODEL, "memory-model")
        finally:
            for key, value in original.items():
                memory_extractor.apply_runtime_config(key, value)

    async def test_retries_when_reasoning_has_no_json_array(self):
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "这段对话值得记录，但输出在最终 JSON 前被截断"}}]
        }
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '[{"content":"我记得这次重要互动","importance":8,"entities":[]}]'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.extract_memories(
                [{"role": "user", "content": "请记住这次互动"}], existing_memories=[]
            )

        self.assertEqual(result[0]["content"], "我记得这次重要互动")
        self.assertEqual(client.post.await_count, 2)
        for call in client.post.await_args_list:
            self.assertNotIn("max_tokens", call.kwargs["json"])
        self.assertIn("只返回最终 JSON 数组", client.post.await_args_list[1].kwargs["json"]["messages"][-1]["content"])

    async def test_failed_request_is_distinct_from_successful_empty_result(self):
        failed = Mock()
        failed.status_code = 503
        failed.text = "unavailable"

        empty = Mock()
        empty.status_code = 200
        empty.json.return_value = {"choices": [{"message": {"content": "[]"}}]}

        client = AsyncMock()
        client.post.side_effect = [failed, empty]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            failed_result = await memory_extractor.extract_memories(
                [{"role": "user", "content": "第一次"}], existing_memories=[]
            )
            empty_result = await memory_extractor.extract_memories(
                [{"role": "user", "content": "第二次"}], existing_memories=[]
            )

        self.assertIsNone(failed_result)
        self.assertEqual(empty_result, [])

    async def test_cognitive_draft_prompt_is_limited_to_one_subject(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        client = AsyncMock()
        client.post.return_value = response

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.generate_cognitive_draft(
                [{"id": 1, "content": "证据", "layer": 1, "importance": 8}],
                [
                    {"subject": "user", "cognitive_type": "user_recent_state", "content": "用户旧认知"},
                    {"subject": "self", "cognitive_type": "self_growth_lesson", "content": "自我旧认知"},
                ],
                "self",
            )

        self.assertEqual(result, [])
        prompt = client.post.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("self_identity_commitment", prompt)
        self.assertIn("self_growth_lesson", prompt)
        self.assertIn("自我旧认知", prompt)
        self.assertNotIn("user_traits_preferences", prompt)
        self.assertNotIn("relationship_change", prompt)
        self.assertNotIn("用户旧认知", prompt)

    async def test_cognitive_draft_rejects_unknown_subject(self):
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"):
            result = await memory_extractor.generate_cognitive_draft(
                [{"id": 1, "content": "证据"}], [], "unknown",
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
