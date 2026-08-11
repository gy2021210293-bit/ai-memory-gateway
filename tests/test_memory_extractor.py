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

    def test_entity_snapshot_guidance_pins_subject_identity(self):
        guidance = memory_extractor.ENTITY_OUTPUT_GUIDANCE
        # 栖 = AI/第一人称；晏晏 = 用户
        self.assertIn("WHO IS WHO: 我是栖", guidance)
        self.assertIn("「用户:」前缀的消息是", guidance)
        self.assertIn("快照 `state` 和记忆 JSON 里的\"我\"一律指栖", guidance)
        # 主语必须与证据一致，禁止张冠李戴（栖的事写成晏晏的）
        self.assertIn("SUBJECT ATTRIBUTION IS CRITICAL", guidance)
        self.assertIn("禁止张冠李戴", guidance)
        self.assertIn("不要用「栖」称呼", guidance)
        self.assertIn("晏晏 (or 她)", guidance)
        # 不得把用户消息里的第一人称"我..."直接抄进 state（输出里"我"会被读作栖）
        self.assertIn("do NOT copy a first-person", guidance)
        self.assertIn('"我" would mean 栖', guidance)
        # 快照不只是一次性事件也收录关键节点，门槛不能过严
        self.assertIn("landmark milestone", guidance)
        self.assertIn("毕业、入职、搬家", guidance)
        self.assertIn("do not skip a milestone", guidance)

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

    async def test_entity_profile_retries_when_reasoning_has_no_json_object(self):
        entity = {"name": "测试", "entity_type": "person", "aliases": [], "profile_json": None}
        memories = [{"id": 1, "content": "证据记忆", "layer": 1}]
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "这些证据值得整理，但最终 JSON 在思考中被截断"}}]
        }
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '{"summary":"我对测试的稳定认识","relationship":"伙伴","stable_facts":["喜欢阅读"],"recent_updates":[],"preferences":[],"uncertainties":[],"evidence_memory_ids":[1]}'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.generate_entity_profile(entity, memories)

        self.assertEqual(result["summary"], "我对测试的稳定认识")
        self.assertEqual(result["stable_facts"], ["喜欢阅读"])
        self.assertEqual(result["evidence_memory_ids"], [1])
        self.assertEqual(client.post.await_count, 2)
        for call in client.post.await_args_list:
            self.assertNotIn("max_tokens", call.kwargs["json"])
        self.assertIn("只返回最终 JSON 对象", client.post.await_args_list[1].kwargs["json"]["messages"][-1]["content"])

    async def test_entity_profile_retries_when_response_content_is_empty(self):
        entity = {"name": "测试", "entity_type": "person", "aliases": [], "profile_json": None}
        memories = [{"id": 1, "content": "证据记忆", "layer": 1}]
        first = Mock()
        first.status_code = 200
        first.json.return_value = {"choices": [{"message": {"content": None}}]}
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '{"summary":"空返回后重试成功","relationship":"伙伴","stable_facts":[],"recent_updates":[],"preferences":[],"uncertainties":[],"evidence_memory_ids":[1]}'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.generate_entity_profile(entity, memories)

        self.assertEqual(result["summary"], "空返回后重试成功")
        self.assertEqual(client.post.await_count, 2)

    async def test_entity_backfill_retries_when_reasoning_has_no_json_array(self):
        memories = [{"id": 1, "content": "旧记忆"}]
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "先想想，输出在最终 JSON 前被截断"}}]
        }
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '[{"memory_id": 1, "entities": [{"name": "明月", "type": "place", "confidence": 0.9}]}]'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.extract_entities_from_memories(memories)

        self.assertEqual(result[1][0]["name"], "明月")
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

    async def test_cognitive_draft_prompt_reviews_all_four_sections(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        client = AsyncMock()
        client.post.return_value = response

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.generate_cognitive_draft(
                [{"id": 1, "content": "证据", "layer": 1, "importance": 8,
                  "created_at": "2026-07-30T08:00:00+00:00"}],
                [
                    {"subject": "user", "cognitive_type": "user_core", "content": "用户旧认知"},
                    {"subject": "self", "cognitive_type": "self_core", "content": "自我旧认知"},
                ],
            )

        self.assertEqual(result, [])
        prompt = client.post.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("user_core", prompt)
        self.assertIn("self_core", prompt)
        self.assertIn("relationship_core", prompt)
        self.assertIn("current_field", prompt)
        self.assertIn("自我旧认知", prompt)
        self.assertIn("用户旧认知", prompt)
        self.assertIn("只有在证据足以形成新认知", prompt)
        self.assertIn("不能提出删除", prompt)

    async def test_cognitive_draft_requires_memories(self):
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"):
            result = await memory_extractor.generate_cognitive_draft(
                [], [],
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
