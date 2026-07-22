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
