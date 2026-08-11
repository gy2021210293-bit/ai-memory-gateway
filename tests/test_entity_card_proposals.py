import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


asyncpg = types.ModuleType("asyncpg")
asyncpg.Pool = object
sys.modules.setdefault("asyncpg", asyncpg)

jieba = types.ModuleType("jieba")
jieba.__path__ = []
jieba.logging = logging
jieba.setLogLevel = lambda _level: None
jieba.add_word = lambda _word: None
jieba_analyse = types.ModuleType("jieba.analyse")
jieba_analyse.extract_tags = lambda text, topK=10: [text]
jieba.analyse = jieba_analyse
sys.modules.setdefault("jieba", jieba)
sys.modules.setdefault("jieba.analyse", jieba_analyse)

import database
import memory_extractor


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


class ProposalConnection:
    def __init__(self, proposal_row=None, entity_row=True, evidence_valid=True, card_json=None,
                 execute_result="UPDATE 1"):
        # entity_row: True → an entity exists (id=1); None → entity missing.
        self.proposal_row = proposal_row
        self.entity_row = entity_row
        self.evidence_valid = evidence_valid
        self.card_json = card_json if card_json is not None else {"description": "", "snapshots": []}
        self.execute_result = execute_result
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, sql, *_args):
        if "FOR UPDATE" in sql:
            return self.proposal_row
        if "entity_card_json" in sql:
            return {"entity_card_json": self.card_json}
        if "memory_entities" in sql:
            return {"id": 1} if self.evidence_valid else None
        if self.entity_row is None:
            return None
        return {"id": 1} if self.entity_row is True else self.entity_row

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return self.execute_result


class EntityCardHarnessTests(unittest.TestCase):
    def test_normalize_snapshot_cleans_and_validates(self):
        raw = {"state": "  住在上海  ", "fact_date": "2026-07-10", "evidence_quote": "“我现在住在上海”"}
        snap = memory_extractor.normalize_entity_snapshot(raw)
        self.assertEqual(snap["state"], "住在上海")
        self.assertEqual(snap["fact_date"], "2026-07-10")
        self.assertEqual(snap["evidence_quote"], "我现在住在上海")

    def test_normalize_snapshot_rejects_bad_date_and_missing_state(self):
        self.assertIsNone(memory_extractor.normalize_entity_snapshot({"state": "", "fact_date": "2026-07-10"}))
        self.assertIsNone(memory_extractor.normalize_entity_snapshot(None))
        bad = memory_extractor.normalize_entity_snapshot({"state": "x", "fact_date": "2026-13-40"})
        self.assertIsNone(bad["fact_date"])

    def test_verbatim_evidence_must_be_user_and_verbatim(self):
        messages = [
            {"id": 1, "role": "assistant", "content": "我现在住在上海"},
            {"id": 2, "role": "user", "content": "我现在住在上海，做设计"},
            {"id": 3, "role": "user", "content": "我喜欢爵士乐"},
        ]
        self.assertEqual(memory_extractor.find_verbatim_evidence_message("现在住在上海", messages)["id"], 2)
        # 改写（非逐字）不匹配
        self.assertIsNone(memory_extractor.find_verbatim_evidence_message("我住在上海做设计", messages))
        # 短句低于最小长度
        self.assertIsNone(memory_extractor.find_verbatim_evidence_message("短", messages))

    def test_verbatim_evidence_ignores_assistant_only(self):
        messages = [{"id": 1, "role": "assistant", "content": "她现在住在上海"}]
        self.assertIsNone(memory_extractor.find_verbatim_evidence_message("她现在住在上海", messages))

    def test_classify_accepts_explicit_user_fact(self):
        messages = [
            {"id": 2, "role": "user", "content": "我今天搬到上海住了"},
            {"id": 3, "role": "user", "content": "现在住在上海"},
        ]
        verdict = memory_extractor.classify_snapshot_suggestion(
            {"state": "住在上海", "fact_date": "2026-07-10", "evidence_quote": "现在住在上海"},
            messages,
        )
        self.assertEqual(verdict[0], "accept")
        self.assertEqual(verdict[1], 3)
        self.assertEqual(verdict[2], "2026-07-10")

    def test_classify_proposal_for_inference_or_unproven(self):
        messages = [{"id": 2, "role": "user", "content": "最近心情不错"}]
        # 推断性描述 + 非逐字短句 → proposal
        self.assertEqual(
            memory_extractor.classify_snapshot_suggestion(
                {"state": "性格开朗", "fact_date": "2026-07-10", "evidence_quote": "她性格挺开朗的"},
                messages,
            )[0],
            "proposal",
        )
        # 无证据短句 → proposal
        self.assertEqual(
            memory_extractor.classify_snapshot_suggestion(
                {"state": "住在上海", "fact_date": "2026-07-10"}, messages,
            )[0],
            "proposal",
        )
        # 日期无效/缺失 → proposal
        self.assertEqual(
            memory_extractor.classify_snapshot_suggestion(
                {"state": "住在上海", "fact_date": "", "evidence_quote": "最近心情不错"}, messages,
            )[0],
            "proposal",
        )
        # 空快照 → proposal
        self.assertEqual(
            memory_extractor.classify_snapshot_suggestion({}, messages)[0],
            "proposal",
        )

    def test_classify_proposal_when_quote_only_in_assistant(self):
        messages = [{"id": 1, "role": "assistant", "content": "她现在住在上海"}]
        self.assertEqual(
            memory_extractor.classify_snapshot_suggestion(
                {"state": "住在上海", "fact_date": "2026-07-10", "evidence_quote": "她现在住在上海"},
                messages,
            )[0],
            "proposal",
        )


class EntityCardProposalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_proposal_inserts(self):
        conn = ProposalConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.create_entity_card_proposal(
                1, "住在上海", "2026-07-10", 7, 20, "user", "无逐字证据",
            )
        self.assertEqual(result["status"], "ok")
        insert_calls = [call for call in conn.execute_calls if "INSERT INTO entity_card_proposals" in call[0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0][1][1], "住在上海")

    async def test_create_proposal_missing_entity(self):
        conn = ProposalConnection(entity_row=None)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.create_entity_card_proposal(999, "住在上海", "2026-07-10")
        self.assertIn("error", result)

    async def test_accept_proposal_validates_and_applies_snapshot(self):
        proposal = {
            "id": 3, "entity_id": 1, "state": "住在上海", "fact_date": "2026-07-10",
            "evidence_memory_id": 7, "evidence_message_id": 20, "status": "pending",
        }
        conn = ProposalConnection(proposal_row=proposal, evidence_valid=True)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.accept_entity_card_proposal(3)
        self.assertEqual(result["status"], "ok")
        accepted_updates = [call for call in conn.execute_calls if "SET status = 'accepted'" in call[0]]
        self.assertEqual(len(accepted_updates), 1)
        card_updates = [call for call in conn.execute_calls if "entity_card_json" in call[0]]
        self.assertEqual(len(card_updates), 1)

    async def test_accept_proposal_rejects_foreign_evidence(self):
        proposal = {
            "id": 3, "entity_id": 1, "state": "住在上海", "fact_date": "2026-07-10",
            "evidence_memory_id": 7, "evidence_message_id": 20, "status": "pending",
        }
        conn = ProposalConnection(proposal_row=proposal, evidence_valid=False)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.accept_entity_card_proposal(3)
        self.assertIn("证据记忆", result["error"])
        self.assertFalse([call for call in conn.execute_calls if "SET status = 'accepted'" in call[0]])

    async def test_accept_proposal_already_decided(self):
        proposal = {
            "id": 3, "entity_id": 1, "state": "住在上海", "fact_date": "2026-07-10",
            "evidence_memory_id": None, "evidence_message_id": None, "status": "rejected",
        }
        conn = ProposalConnection(proposal_row=proposal)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.accept_entity_card_proposal(3)
        self.assertIn("已处理", result["error"])

    async def test_reject_proposal(self):
        conn = ProposalConnection(execute_result="UPDATE 1")
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.reject_entity_card_proposal(3)
        self.assertEqual(result["status"], "ok")
        update_calls = [call for call in conn.execute_calls if "SET status = 'rejected'" in call[0]]
        self.assertEqual(len(update_calls), 1)

    async def test_reject_proposal_missing_or_decided(self):
        conn = ProposalConnection(execute_result="UPDATE 0")
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.reject_entity_card_proposal(3)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
