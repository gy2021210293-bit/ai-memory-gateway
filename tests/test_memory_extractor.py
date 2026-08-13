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
        self.assertIn("Demo@2026Passw0rd", prompt)
        self.assertIn("# 简短示例", prompt)
        self.assertIn("一条碎片只记一件事", prompt)
        self.assertIn("8-10（high）", prompt)
        self.assertIn('"content": "我以第一人称记住的内容"', prompt)
        self.assertNotIn('"mood":', prompt)
        self.assertNotIn('"topics":', prompt)

    def test_fragment_guidance_keeps_identity_and_drops_state(self):
        # 碎片只提取实体身份：实体数组/别名/置信度保留，状态快照指引移除（状态由事件层提取）
        guidance = memory_extractor.ENTITY_OUTPUT_GUIDANCE
        self.assertIn("For each returned memory, include an `entities` array", guidance)
        self.assertIn('"name":"display name"', guidance)
        self.assertIn("aliases", guidance)
        self.assertNotIn("snapshot", guidance)
        self.assertNotIn("WHO IS WHO", guidance)

    def test_state_subject_attribution_lives_in_snapshot_backfill_prompt(self):
        # 状态/主语归属规则仍在实体卡回填 prompt（Dashboard 手动补卡 → 提案）
        prompt = memory_extractor._build_snapshot_backfill_prompt([
            {"id": 1, "name": "小明", "entity_type": "person", "aliases": [], "memories": []}
        ])
        self.assertIn("「我」指栖（我自己）", prompt)
        self.assertIn("「她」指晏晏（用户）", prompt)
        self.assertIn("绝不可张冠李戴", prompt)
        self.assertIn("禁止出现「用户」", prompt)

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

    async def test_cognitive_draft_prompt_reviews_all_three_scopes(self):
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
                    {"subject": "user", "cognitive_type": "user_core", "content": "用户旧认知",
                     "level": "explicit", "times_derived": 1, "evidence_memory_ids": [1], "id": 10},
                    {"subject": "self", "cognitive_type": "self_core", "content": "自我旧认知",
                     "level": "deductive", "times_derived": 2, "evidence_memory_ids": [1], "id": 11},
                ],
            )

        self.assertEqual(result, [])
        prompt = client.post.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("user_core", prompt)
        self.assertIn("self_core", prompt)
        self.assertIn("relationship_core", prompt)
        self.assertNotIn("current_field", prompt)
        self.assertIn("card_id=10", prompt)
        self.assertIn("card_id=11", prompt)
        self.assertIn("自我旧认知", prompt)
        self.assertIn("用户旧认知", prompt)
        self.assertIn("原子化：每条候选只陈述一个自包含的认知", prompt)
        self.assertIn("reinforce / supersede / conflict 的 target_id 必须指向同区块的 active 卡", prompt)
        self.assertIn("不能提出删除", prompt)

    async def test_cognitive_draft_prompt_covers_conflict_stability_and_evidence_independence(self):
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
                    {"subject": "user", "cognitive_type": "user_core", "content": "用户当前状态",
                     "level": "explicit", "times_derived": 1, "evidence_memory_ids": [1],
                     "id": 10, "review_after": "2026-08-13"},
                ],
            )

        self.assertEqual(result, [])
        prompt = client.post.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("conflict 冲突", prompt)
        self.assertIn("同一次对话里的复述不算多份独立证据", prompt)
        self.assertIn("稳定度（review_after）", prompt)
        self.assertIn("[当前]", prompt)  # 现有卡带稳定度标记
        self.assertIn("不要为“正在聊的话题/主题”建卡", prompt)
        self.assertIn("候选内容不得与任一区块现有 active 卡实质重复", prompt)

    async def test_cognitive_draft_prompt_feeds_back_human_revisions(self):
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
                [],
                [
                    {"id": 2, "card_id": None, "subject": "user",
                     "cognitive_type": "user_core", "action": "reject",
                     "content_before": "被拒绝的认知", "content_after": None,
                     "level_before": None, "level_after": None,
                     "created_at": "2026-08-01T08:00:00+00:00"},
                    {"id": 3, "card_id": 7, "subject": "user",
                     "cognitive_type": "user_core", "action": "edit",
                     "content_before": "旧版本", "content_after": "修正后版本",
                     "level_before": "explicit", "level_after": "deductive",
                     "created_at": "2026-08-02T08:00:00+00:00"},
                ],
            )

        self.assertEqual(result, [])
        prompt = client.post.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("人工近期确认/修正记录", prompt)
        self.assertIn("人工拒绝", prompt)
        self.assertIn("被拒绝的认知", prompt)
        self.assertIn("人工修正", prompt)
        self.assertIn("旧版本 → 修正后版本", prompt)
        self.assertIn("不得重新提出已被人工删除或拒绝的认知", prompt)

    async def test_cognitive_draft_requires_memories(self):
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"):
            result = await memory_extractor.generate_cognitive_draft(
                [], [],
            )
        self.assertIsNone(result)

    async def test_cognitive_draft_retries_when_reasoning_has_no_json_array(self):
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "先整体审视三元一场，但最终 JSON 在思考中被截断"}}]
        }
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '[{"subject":"user","cognitive_type":"user_core","content":"晏晏重视工作与生活的平衡","level":"inductive","confidence":0.5,"evidence_memory_ids":[1],"action":"create"}]'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]

        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.generate_cognitive_draft(
                [{"id": 1, "content": "证据", "layer": 1, "importance": 8,
                  "created_at": "2026-07-30T08:00:00+00:00"}],
                [],
            )

        self.assertEqual(result[0]["action"], "create")
        self.assertEqual(client.post.await_count, 2)
        for call in client.post.await_args_list:
            self.assertNotIn("max_tokens", call.kwargs["json"])
        self.assertIn("只返回最终 JSON 数组", client.post.await_args_list[1].kwargs["json"]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
