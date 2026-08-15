"""半自动分级认知审视：自动应用 / 挂起待确认的决策规则测试。"""
import unittest
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
    async def test_high_confidence_explicit_create_is_applied(self):
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.8, level="explicit"))
        self.assertEqual(outcome, "applied")
        save_mock.assert_awaited_once()
        self.assertEqual(save_mock.await_args.kwargs["created_by"], "auto")

    async def test_low_confidence_create_is_queued(self):
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.55, level="explicit"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_inductive_create_is_queued_even_when_high_confidence(self):
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.85, level="inductive"))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_reinforce_is_applied(self):
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"status": "ok"})) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="reinforce", target_id=5))
        self.assertEqual(outcome, "applied")
        save_mock.assert_awaited_once()

    async def test_supersede_auto_target_is_applied(self):
        with (
            patch.object(main, "get_cognitive_item",
                         AsyncMock(return_value={"id": 5, "created_by": "auto"})),
            patch.object(main, "save_cognitive_item",
                         AsyncMock(return_value={"status": "ok"})),
        ):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="supersede", target_id=5, confidence=0.8))
        self.assertEqual(outcome, "applied")

    async def test_supersede_human_target_is_queued(self):
        with (
            patch.object(main, "get_cognitive_item",
                         AsyncMock(return_value={"id": 5, "created_by": "manual"})),
            patch.object(main, "save_cognitive_item", AsyncMock()),
        ):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="supersede", target_id=5, confidence=0.9))
        self.assertEqual(outcome, "queued")

    async def test_supersede_low_confidence_auto_target_is_queued(self):
        with (
            patch.object(main, "get_cognitive_item",
                         AsyncMock(return_value={"id": 5, "created_by": "auto"})),
            patch.object(main, "save_cognitive_item", AsyncMock()),
        ):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="supersede", target_id=5, confidence=0.5))
        self.assertEqual(outcome, "queued")

    async def test_supersede_missing_target_is_queued(self):
        with (
            patch.object(main, "get_cognitive_item", AsyncMock(return_value=None)),
            patch.object(main, "save_cognitive_item", AsyncMock()),
        ):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="supersede", target_id=5, confidence=0.9))
        self.assertEqual(outcome, "queued")

    async def test_conflict_is_always_queued(self):
        with patch.object(main, "save_cognitive_item", AsyncMock()) as save_mock:
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(action="conflict", target_id=5))
        self.assertEqual(outcome, "queued")
        save_mock.assert_not_awaited()

    async def test_save_error_falls_back_to_queue(self):
        with patch.object(main, "save_cognitive_item",
                          AsyncMock(return_value={"error": "该区块已存在相同内容的认知"})):
            outcome = await main._auto_apply_or_queue_candidate(
                _candidate(confidence=0.8, level="explicit"))
        self.assertEqual(outcome, "queued")


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

    async def test_full_cycle_applies_and_queues(self):
        applied = _candidate(confidence=0.85, level="explicit")
        queued = _candidate(confidence=0.5, level="explicit")
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


if __name__ == "__main__":
    unittest.main()
