"""半自动分级认知审视：自动应用 / 挂起待确认的决策规则测试。"""
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import main


def _candidate(action="create", confidence=0.8, level="explicit", target_id=None):
    return {
        "subject": "user", "cognitive_type": "user_core",
        "content": "候选认知", "confidence": confidence,
        "level": level, "action": action, "target_id": target_id,
        "evidence_memory_ids": [12], "review_after": None,
    }


class AutoApplyDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_is_always_queued_even_when_high_confidence(self):
        # 人工审核门控：AI 生成的 create 一律进待确认队列，不自动应用
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.95, level="explicit"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_create_is_queued_even_when_low_confidence(self):
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.55, level="explicit"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_deductive_create_is_queued(self):
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.85, level="deductive"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_inductive_create_is_queued(self):
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.85, level="inductive"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_reinforce_is_applied(self):
        # reinforce 非破坏性（内容不变，仅累计证据）：仍自动应用
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="reinforce", target_id=5))
        self.assertEqual(outcome, "applied")
        save_mock.assert_awaited_once()

    async def test_supersede_is_always_queued(self):
        # supersede 生成新卡：一律挂起待人工确认
        with (
            patch.object(main, "get_cognitive_item",
                         AsyncMock(return_value={"id": 5, "created_by": "auto"})),
            patch.object(main, "save_cognitive_item",
                         AsyncMock(return_value={"status": "ok"})),
        ):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="supersede", target_id=5, confidence=0.95))
        self.assertEqual(outcome, "queued")

    async def test_conflict_is_always_queued(self):
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="conflict", target_id=5))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_save_error_falls_back_to_queue(self):
        # reinforce 保存失败 → 跳过（不占队列）
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"error": "强化需保持内容一致"})):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="reinforce", target_id=5))
        self.assertEqual(outcome, "skipped")


class AutoReviewRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_mode_is_noop(self):
        with (
            patch.object(main, "get_gateway_config",
                         AsyncMock(return_value="manual")),
            patch.object(main, "_build_cognitive_draft", AsyncMock()) as draft_mock,
        ):
            result = await main.run_cognitive_auto_review_once()
            self.assertEqual(result["status"], "disabled")
            draft_mock.assert_not_awaited()

    async def test_no_new_memories_is_noop(self):
        with (
            patch.object(main, "get_gateway_config",
                         AsyncMock(return_value="auto")),
            patch.object(main, "_build_cognitive_draft",
                         AsyncMock(return_value={"ok": False,
                                                "error": "没有新的记忆可审视"})),
        ):
            result = await main.run_cognitive_auto_review_once()
        self.assertEqual(result["status"], "noop")

    async def test_full_cycle_applies_reinforce_and_queues_rest(self):
        applied = _candidate(action="reinforce", target_id=5)
        queued = _candidate(confidence=0.9, level="explicit")
        with (
            patch.object(main, "get_gateway_config",
                         AsyncMock(return_value="auto")),
            patch.object(main, "_build_cognitive_draft",
                         AsyncMock(return_value={
                             "ok": True, "items": [applied, queued],
                             "memories": [{"id": 1}, {"id": 2}],
                             "cursor": 2, "model": "test-model",
                         })),
            patch.object(main, "save_cognitive_item",
                         AsyncMock(return_value={"status": "ok"})),
            patch.object(main, "queue_cognitive_pending",
                         AsyncMock(return_value=77)) as queue_mock,
        ):
            result = await main.run_cognitive_auto_review_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["applied"], 1)
            self.assertEqual(result["queued"], 1)
            queue_mock.assert_awaited_once_with(queued)

    async def test_full_cycle_full_cell_create_goes_to_pending(self):
        # 满员区块的 create 自动应用不放行 → 挂起待人工确认（信息不丢）
        full_cell_create = _candidate(confidence=0.9, level="explicit")
        with (
            patch.object(main, "get_gateway_config",
                         AsyncMock(return_value="auto")),
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=[
                             {"id": 1, "subject": "user", "cognitive_type": "user_core",
                              "content": "存量卡", "status": "active"},
                         ] + [
                             {"id": i, "subject": "user", "cognitive_type": "user_core",
                              "content": f"存量卡{i}", "status": "active"}
                             for i in range(2, 6)
                         ])),
            patch.object(main, "_build_cognitive_draft",
                         AsyncMock(return_value={
                             "ok": True, "items": [full_cell_create],
                             "memories": [{"id": 1}],
                             "cursor": 1, "model": "test-model",
                         })),
            patch.object(main, "save_cognitive_item",
                         AsyncMock(return_value={"status": "ok"})),
            patch.object(main, "queue_cognitive_pending",
                         AsyncMock(return_value=77)) as queue_mock,
        ):
            result = await main.run_cognitive_auto_review_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["applied"], 0)
            self.assertEqual(result["queued"], 1)
            queue_mock.assert_awaited_once_with(full_cell_create)


class CognitiveInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_cognitive_text_uses_reviewed_only_gate(self):
        # 注入端只取人工审核过的卡（reviewed_only=True）：AI 自动生成的认知不注入
        with (
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=[])) as list_mock,
            patch.object(main, "format_cognitive_items_for_prompt",
                         return_value="X") as fmt_mock,
        ):
            text = await main.build_cognitive_text("旅行", [10, 11])
        self.assertEqual(text, "X")
        self.assertEqual(list_mock.await_args.kwargs.get("reviewed_only"), True)
        self.assertNotIn("context", fmt_mock.call_args.kwargs)

    async def test_build_cognitive_text_without_args_is_plain_full_injection(self):
        with (
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=[])) as list_mock,
            patch.object(main, "format_cognitive_items_for_prompt",
                         return_value="Y") as fmt_mock,
        ):
            text = await main.build_cognitive_text()
        self.assertEqual(text, "Y")
        self.assertEqual(list_mock.await_args.kwargs.get("reviewed_only"), True)
        self.assertNotIn("context", fmt_mock.call_args.kwargs)

    async def test_build_memory_text_returns_text_and_ids(self):
        with (
            patch.object(main, "find_directly_mentioned_entities",
                         AsyncMock(return_value=[])),
            patch.object(main, "search_memories", AsyncMock(return_value=[
                {"id": 3, "content": "第一条", "layer": 1,
                 "created_at": None, "entities": []},
                {"id": 7, "content": "第二条", "layer": 2,
                 "created_at": None, "entities": []},
            ])),
            patch.object(main, "_build_entity_overview",
                         AsyncMock(return_value="")),
        ):
            result = await main.build_memory_text("你好")
        self.assertEqual(result["memory_ids"], [3, 7])
        self.assertIn("retrieved_memories", result["text"])

    async def test_build_memory_text_failure_returns_empty_dict(self):
        with patch.object(main, "find_directly_mentioned_entities",
                          AsyncMock(side_effect=RuntimeError("db down"))):
            result = await main.build_memory_text("你好")
        self.assertEqual(result, {"text": "", "memory_ids": []})


class CognitiveDraftPipelineTests(unittest.IsolatedAsyncioTestCase):
    """草稿管线：语义去重（重复即强化/跨区块丢弃）+ 满员 create 不丢弃（留给人工或挂起）。"""

    def _active_cards(self, count, content_prefix="卡"):
        return [
            {"id": i, "subject": "user", "cognitive_type": "user_core",
             "content": f"{content_prefix}{i}", "status": "active"}
            for i in range(1, count + 1)
        ]

    def _create_candidate(self, content="新认知", subject="user",
                          cognitive_type="user_core"):
        return {
            "subject": subject, "cognitive_type": cognitive_type,
            "content": content, "level": "explicit", "confidence": 0.9,
            "evidence_memory_ids": [1], "action": "create",
        }

    async def _run_pipeline(self, current_items, raw_draft, memory_ids=(1, 2),
                            embeddings=None, deep=True):
        """默认以深度模式验证完整认知生命周期；快速模式另行覆盖。"""
        from contextlib import ExitStack
        patches = [
            patch.object(main,
                         "get_memories_for_portrait_review" if deep else "get_memories_for_cognitive_draft",
                         AsyncMock(return_value=[{"id": mid} for mid in memory_ids])),
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=current_items)),
            patch.object(main, "get_recent_cognitive_revisions",
                         AsyncMock(return_value=[])),
            patch.object(main, "generate_cognitive_draft",
                         AsyncMock(return_value=raw_draft)),
            patch.object(main,
                         "advance_cognitive_deep_review_cursor" if deep else "advance_cognitive_draft_cursor",
                         AsyncMock(return_value=max(memory_ids))),
        ]
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            if embeddings is not None:
                stack.enter_context(patch.object(
                    main, "compute_embeddings_batch",
                    AsyncMock(return_value=embeddings)))
            return await main._build_cognitive_draft(deep=deep)

    async def test_create_stays_in_draft_when_cell_is_full(self):
        # 满员 create 不再丢弃：留在草稿里给人工看（或由自动审视挂起），信息不丢
        result = await self._run_pipeline(self._active_cards(5),
                                          [self._create_candidate()])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["content"], "新认知")

    async def test_create_is_allowed_when_cell_has_room(self):
        result = await self._run_pipeline(self._active_cards(4),
                                          [self._create_candidate()])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["content"], "新认知")

    async def test_fast_pipeline_drops_stable_create_but_keeps_current_create(self):
        stable = self._create_candidate("稳定画像")
        current = self._create_candidate("近期状态")
        current["review_after"] = "2026-09-01"
        result = await self._run_pipeline([], [stable, current], deep=False)
        self.assertEqual([item["content"] for item in result["items"]], ["近期状态"])

    async def test_fast_pipeline_only_supersedes_current_cards(self):
        stable_target = {"id": 3, "subject": "user", "cognitive_type": "user_core",
                         "content": "稳定认知", "status": "active", "review_after": None}
        current_target = {"id": 4, "subject": "user", "cognitive_type": "user_core",
                          "content": "近期认知", "status": "active", "review_after": "2026-08-20"}
        stable_change = self._create_candidate("稳定认知更新")
        stable_change.update({"action": "supersede", "target_id": 3})
        current_change = self._create_candidate("近期认知更新")
        current_change.update({"action": "supersede", "target_id": 4,
                               "review_after": "2026-09-01"})
        result = await self._run_pipeline(
            [stable_target, current_target], [stable_change, current_change], deep=False)
        self.assertEqual([item["target_id"] for item in result["items"]], [4])

    async def test_auto_apply_create_is_always_queued_regardless_of_cell_count(self):
        # 人工审核门控：create 一律挂起，与区块是否满员无关
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.9, level="explicit"),
                active_counts={"user_core": 4})
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_pipeline_semantic_duplicate_create_becomes_reinforce(self):
        # 同区块实质重复 → 自动转 reinforce：内容对齐目标卡，target_id 指向该卡
        existing = [{"id": 5, "subject": "user", "cognitive_type": "user_core",
                     "content": "她喜欢安静的环境", "status": "active"}]
        candidate = self._create_candidate("她偏好安静的氛围")
        result = await self._run_pipeline(
            existing, [candidate], embeddings=[[1.0, 0.0], [1.0, 0.0]])
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "reinforce")
        self.assertEqual(items[0]["target_id"], 5)
        self.assertEqual(items[0]["content"], "她喜欢安静的环境")

    async def test_pipeline_drops_create_duplicating_other_cell(self):
        # 跨区块重复（尤其自我认知 vs 关系认知）→ 丢弃，信息已在别处
        existing = [{"id": 5, "subject": "self", "cognitive_type": "self_core",
                     "content": "我是她愿意倾诉的对象", "status": "active"}]
        candidate = self._create_candidate(
            "她把我当作倾诉对象", subject="relationship",
            cognitive_type="relationship_core")
        result = await self._run_pipeline(
            existing, [candidate], embeddings=[[1.0, 0.0], [1.0, 0.0]])
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])  # 与其它区块重复：跳过

    async def test_pipeline_keeps_create_when_no_semantic_duplicate(self):
        # 无向量（未配置 key）且字符相似度不够 → 保持 create
        existing = [{"id": 5, "subject": "user", "cognitive_type": "user_core",
                     "content": "她喜欢爬山", "status": "active"}]
        candidate = self._create_candidate("她最近在准备影展")
        result = await self._run_pipeline(existing, [candidate])
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "create")

    async def test_deep_pipeline_uses_portrait_review_evidence(self):
        # deep=True（深度体检）：走历史抽样证据查询，并传给生成函数 deep 标记
        with (
            patch.object(main, "get_memories_for_portrait_review",
                         AsyncMock(return_value=[{"id": 5, "content": "历史记忆", "layer": 1,
                                                  "importance": 8, "created_at": None}])),
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=[])),
            patch.object(main, "get_recent_cognitive_revisions",
                         AsyncMock(return_value=[])),
            patch.object(main, "generate_cognitive_draft",
                         AsyncMock(return_value=[self._create_candidate()])) as gen_mock,
            patch.object(main, "advance_cognitive_deep_review_cursor",
                         AsyncMock(return_value=5)),
        ):
            result = await main._build_cognitive_draft(deep=True)
        self.assertTrue(result["ok"])
        self.assertEqual(gen_mock.await_args.kwargs.get("deep"), True)

    async def test_pipeline_keeps_merge_candidate_with_valid_targets(self):
        existing = [
            {"id": 3, "subject": "user", "cognitive_type": "user_core",
             "content": "喜欢安静", "status": "active"},
            {"id": 4, "subject": "user", "cognitive_type": "user_core",
             "content": "偏好独处", "status": "active"},
        ]
        merge_candidate = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "偏好安静与独处", "level": "explicit", "confidence": 0.8,
            "evidence_memory_ids": [1], "action": "merge", "target_ids": [3, 4],
        }
        result = await self._run_pipeline(existing, [merge_candidate])
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "merge")
        self.assertEqual(items[0]["target_ids"], [3, 4])

    async def test_pipeline_drops_merge_with_invalid_target(self):
        existing = [{"id": 3, "subject": "user", "cognitive_type": "user_core",
                     "content": "喜欢安静", "status": "active"}]
        merge_candidate = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "偏好安静", "level": "explicit", "confidence": 0.8,
            "evidence_memory_ids": [1], "action": "merge", "target_ids": [3, 999],
        }
        result = await self._run_pipeline(existing, [merge_candidate])
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])  # 目标无效：整条丢弃

    async def test_evidence_relevance_drops_unrelated_evidence(self):
        # 证据相关性校验：与卡内容向量不相关的证据被剔除（防 AI 瞎挂证据）
        candidate = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "掌控感驱动：对数据所有权有高需求", "level": "explicit",
            "confidence": 0.9, "evidence_memory_ids": [1, 2], "action": "create",
        }
        # 卡向量 [1,0]：证据1 高度相关（0.99），证据2 完全不相关（0）
        result = await self._run_pipeline(
            [], [candidate],
            embeddings=[[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]],
        )
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["evidence_memory_ids"], [1])

    async def test_evidence_all_irrelevant_drops_candidate(self):
        candidate = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "掌控感驱动", "level": "explicit",
            "confidence": 0.9, "evidence_memory_ids": [1, 2], "action": "create",
        }
        result = await self._run_pipeline(
            [], [candidate],
            embeddings=[[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])  # 证据全部不相关 → 丢弃

    async def _run_pipeline_with_gate(self, current_items, raw_draft, gate_rows,
                                      memory_ids=(1, 2)):
        """跑 _build_cognitive_draft，并 mock get_pool 返回证据日期（供 ≥3 跨时间门槛查证）。"""
        from contextlib import ExitStack

        class _GateConn:
            def __init__(self, rows):
                self.rows = rows

            async def fetch(self, query, *_args):
                if "cognitive_corrections" in query:
                    return []
                return self.rows

        patches = [
            patch.object(main, "get_memories_for_portrait_review",
                         AsyncMock(return_value=[{"id": mid} for mid in memory_ids])),
            patch.object(main, "list_cognitive_items",
                         AsyncMock(return_value=current_items)),
            patch.object(main, "get_recent_cognitive_revisions",
                         AsyncMock(return_value=[])),
            patch.object(main, "generate_cognitive_draft",
                         AsyncMock(return_value=raw_draft)),
            patch.object(main, "advance_cognitive_deep_review_cursor",
                         AsyncMock(return_value=max(memory_ids))),
            patch.object(main, "get_pool",
                         return_value=database_fake_pool(_GateConn(gate_rows))),
        ]
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return await main._build_cognitive_draft(deep=True)

    async def test_supersede_upgrade_insufficient_evidence_stays_current(self):
        # 升级意图（supersede 短期卡、不写复核日期）但证据不足 → 保持短期，沿用原复核日期
        target = {"id": 5, "subject": "user", "cognitive_type": "user_core",
                  "content": "最近在忙项目A", "review_after": date(2026, 8, 20),
                  "status": "active"}
        supersede = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "对编程有长期热情，已稳定", "level": "explicit",
            "confidence": 0.85, "evidence_memory_ids": [1, 2],
            "action": "supersede", "target_id": 5,
        }
        # 证据 2 条（<3）→ 不够转长期
        gate_rows = [
            {"id": 1, "created_at": "2026-08-01T00:00:00+00:00"},
            {"id": 2, "created_at": "2026-08-02T00:00:00+00:00"},
        ]
        result = await self._run_pipeline_with_gate(
            [target], [supersede], gate_rows,
        )
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "supersede")
        self.assertEqual(items[0]["review_after"], date(2026, 8, 20))  # 保持短期，沿用旧复核日期

    async def test_supersede_upgrade_sufficient_evidence_becomes_stable(self):
        target = {"id": 5, "subject": "user", "cognitive_type": "user_core",
                  "content": "最近在忙项目A", "review_after": date(2026, 8, 20),
                  "status": "active"}
        supersede = {
            "subject": "user", "cognitive_type": "user_core",
            "content": "对编程有长期热情，已稳定", "level": "explicit",
            "confidence": 0.85, "evidence_memory_ids": [1, 2, 3],
            "action": "supersede", "target_id": 5,
        }
        # 证据 3 条、跨 2 个日期 → 允许升级为长期
        gate_rows = [
            {"id": 1, "created_at": "2026-07-01T00:00:00+00:00"},
            {"id": 2, "created_at": "2026-08-01T00:00:00+00:00"},
            {"id": 3, "created_at": "2026-08-10T00:00:00+00:00"},
        ]
        result = await self._run_pipeline_with_gate(
            [target], [supersede], gate_rows, memory_ids=(1, 2, 3),
        )
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "supersede")
        self.assertIsNone(items[0]["review_after"])  # 升级成功：无复核日期 = 长期


class CognitiveCorrectionTests(unittest.TestCase):
    def test_correction_keyword_detection_positive(self):
        positives = [
            "你记错了，我不喜欢香菜",
            "我说的是周末，不是周三",
            "其实不是这样的",
            "别乱说，我从来没说过那句话",
            "你理解错了",
        ]
        for text in positives:
            self.assertTrue(main._is_cognitive_correction(text), text)

    def test_correction_keyword_detection_negative(self):
        negatives = [
            "今天天气不错",
            "你记性真好",
            "其实我也这么觉得",
            "不是吧，你也去？",
        ]
        for text in negatives:
            self.assertFalse(main._is_cognitive_correction(text), text)

    async def test_record_cognitive_correction_deduplicates(self):
        class _FakeConn:
            def __init__(self):
                self.rows = []
            async def fetchrow(self, _q, _content):
                return self.rows[0] if self.rows else None
            async def fetchval(self, _q, _content):
                self.rows.append({"id": 1})
                return 1

        conn = _FakeConn()
        pool = database_fake_pool(conn)
        with patch.object(main, "get_pool", return_value=pool):
            first = await main.record_cognitive_correction("  你记错了，我不吃香菜 ")
            dup = await main.record_cognitive_correction("你记错了，我不吃香菜")
        self.assertEqual(first, 1)
        self.assertEqual(dup, 0)  # 同内容去重


def database_fake_pool(conn):
    class _Acquire:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *_args):
            return False
    class _Pool:
        def acquire(self):
            return _Acquire()
    return _Pool()


class CognitiveIntegrateScanTests(unittest.IsolatedAsyncioTestCase):
    """整合扫描（确定性）：同区块重叠 → merge，跨区块重复 → retire。"""

    def _card(self, card_id, subject, cognitive_type, content, times_derived=1):
        return {
            "id": card_id, "subject": subject, "cognitive_type": cognitive_type,
            "content": content, "level": "explicit", "times_derived": times_derived,
            "confidence": 0.8, "evidence_memory_ids": [card_id],
            "review_after": None,
        }

    async def test_scan_proposes_merge_for_same_cell_and_retire_for_cross_cell(self):
        cards = [
            self._card(1, "user", "user_core", "喜欢安静", times_derived=2),
            self._card(2, "user", "user_core", "偏好独处", times_derived=1),
            self._card(3, "self", "self_core", "我是她愿意倾诉的对象", times_derived=3),
            self._card(4, "relationship", "relationship_core", "她向我倾诉", times_derived=1),
        ]
        with (
            patch.object(main, "list_cognitive_items", AsyncMock(return_value=cards)),
            patch.object(main, "compute_embeddings_batch",
                         AsyncMock(return_value=[
                             [1.0, 0.0], [1.0, 0.0],   # 卡1/卡2 同区块重叠
                             [0.0, 1.0], [0.0, 1.0],   # 卡3/卡4 跨区块重复
                         ])),
        ):
            result = await main.api_integrate_scan()
        self.assertEqual(result["scan_count"], 2)
        merge = next(it for it in result["items"] if it["action"] == "merge")
        retire = next(it for it in result["items"] if it["action"] == "retire")
        # 同区块：合并，内容取证据更强卡，target_ids 列出两张
        self.assertEqual(merge["target_ids"], [1, 2])
        self.assertEqual(merge["content"], "喜欢安静")
        # 跨区块：退休较弱卡（relationship #4），保留较强卡（self #3）
        self.assertEqual(retire["target_id"], 4)
        self.assertEqual(retire["retain_id"], 3)
        self.assertEqual(retire["action"], "retire")

    async def test_scan_skips_low_similarity_and_returns_empty(self):
        cards = [
            self._card(1, "user", "user_core", "喜欢爬山"),
            self._card(2, "user", "user_core", "最近在准备影展"),
        ]
        with (
            patch.object(main, "list_cognitive_items", AsyncMock(return_value=cards)),
            patch.object(main, "compute_embeddings_batch",
                         AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])),
        ):
            result = await main.api_integrate_scan()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["scan_count"], 0)

    async def test_scan_flags_partial_overlap_with_embedded_chunk(self):
        # 一大段里嵌着另一段的一整块：整段余弦（0.8 < 0.9）漏不掉？不——包含度要能抓到
        cards = [
            self._card(1, "user", "user_core",
                       "身份与形象：她喜欢安静的环境，阳台种了薄荷和茉莉，喜欢摄影，偏爱靠窗的位置",
                       times_derived=2),
            self._card(2, "user", "user_core",
                       "阳台种了薄荷和茉莉，最近在准备影展", times_derived=1),
        ]
        with (
            patch.object(main, "list_cognitive_items", AsyncMock(return_value=cards)),
            patch.object(main, "compute_embeddings_batch",
                         AsyncMock(return_value=[[1.0, 0.0], [0.8, 0.2]])),  # 全局余弦 0.8 < 0.9
        ):
            result = await main.api_integrate_scan()
        self.assertEqual(result["scan_count"], 1)
        merge = result["items"][0]
        self.assertEqual(merge["action"], "merge")
        self.assertEqual(merge["target_ids"], [1, 2])
        self.assertEqual(merge["content"], cards[0]["content"])  # 保留证据更强卡

    async def test_scan_keeps_containing_card_when_one_embedded(self):
        # 单向包含（A 整体嵌在 B 里）→ 保留被包含的较长卡内容
        cards = [
            self._card(1, "user", "user_core", "她喜欢安静的环境"),
            self._card(2, "user", "user_core", "她喜欢安静的环境，阳台种了薄荷和茉莉"),
        ]
        with (
            patch.object(main, "list_cognitive_items", AsyncMock(return_value=cards)),
            patch.object(main, "compute_embeddings_batch",
                         AsyncMock(return_value=[[1.0, 0.0], [1.0, 0.0]])),
        ):
            result = await main.api_integrate_scan()
        self.assertEqual(result["scan_count"], 1)
        merge = result["items"][0]
        self.assertEqual(merge["target_ids"], [2, 1])
        self.assertEqual(merge["content"], cards[1]["content"])  # 保留较长（被包含方）内容


if __name__ == "__main__":
    unittest.main()
