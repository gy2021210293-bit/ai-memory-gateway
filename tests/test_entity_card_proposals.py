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

    def test_resolve_evidence_memory_prefers_verbatim_then_newest(self):
        # 列表按 created_at DESC（最新在前），与 get_entity_memories 一致
        memories = [
            {"id": 7, "content": "我搬到上海了，现在住上海"},  # 最新
            {"id": 6, "content": "小明在备考"},
        ]
        self.assertEqual(memory_extractor._resolve_evidence_memory("我搬到上海了", memories), 7)
        self.assertEqual(memory_extractor._resolve_evidence_memory("没有这句", memories), 7)
        self.assertIsNone(memory_extractor._resolve_evidence_memory("", []))
        self.assertIsNone(memory_extractor._resolve_evidence_memory("引文", []))

    def test_build_snapshot_backfill_prompt_is_bounded(self):
        long_content = "长" * 300
        entities = [{
            "id": 9, "name": "小文", "entity_type": "person",
            "aliases": ["文文", "小W"],
            "memories": [{"id": 1, "content": long_content}, {"id": 2, "content": "住在杭州"}],
        }]
        prompt = memory_extractor._build_snapshot_backfill_prompt(entities)
        self.assertIn("实体 9", prompt)
        self.assertIn("小文（person，别名：文文、小W）", prompt)
        self.assertIn("[ID=2] 住在杭州", prompt)
        self.assertNotIn("长" * 300, prompt)  # 超长记忆被截断
        self.assertIn('"state": null', prompt)


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


class _FakeCardResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


class _FakeCardClient:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeCardResponse(self._payload, status=self._status)


SNAPSHOT_BACKFILL_OK = {
    "choices": [{"message": {"content": (
        '{"1":{"state":" 住在上海 ","fact_date":"2026-07-20","evidence_quote":"我搬到上海了"},'
        '"2":{"state":null},'
        '"3":{"state":"二期进行中","fact_date":"2026-08-01","evidence_quote":"不存在的引文"},'
        '"999":{"state":"无关","fact_date":"2026-07-01","evidence_quote":"x"}}'
    )}}]
}


class EntityCardBackfillAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_suggest_snapshots_batch_parses_and_resolves_evidence(self):
        entities = [
            # memories 按 created_at DESC（最新在前），与 get_entity_memories 一致
            {"id": 1, "name": "小明", "entity_type": "person", "aliases": ["阿明"], "memories": [
                {"id": 7, "content": "小明说：我搬到上海了，现在住上海"},  # 最新
                {"id": 6, "content": "小明正在准备考试"},
            ]},
            {"id": 2, "name": "小猫", "entity_type": "pet", "aliases": [], "memories": []},
            {"id": 3, "name": "项目A", "entity_type": "project", "aliases": [], "memories": [
                {"id": 11, "content": "项目A 二期进行中"},  # 最新
                {"id": 10, "content": "项目A 一期已交付"},
            ]},
        ]
        with patch("memory_extractor.httpx.AsyncClient", lambda **_kw: _FakeCardClient(SNAPSHOT_BACKFILL_OK)), \
             patch("memory_extractor.get_memory_api_key", return_value="key"), \
             patch("memory_extractor.get_memory_api_base_url", return_value="http://test"):
            payload = await memory_extractor.suggest_entity_snapshots_batch(entities)
        self.assertNotIn("error", payload)
        results = payload["results"]
        self.assertEqual(results[1]["state"], "住在上海")
        self.assertEqual(results[1]["fact_date"], "2026-07-20")
        self.assertEqual(results[1]["evidence_memory_id"], 7)   # 逐字引文命中记忆 7
        self.assertNotIn(2, results)                              # state: null → 无建议
        self.assertNotIn(999, results)                            # 批次外实体 → 丢弃
        self.assertEqual(results[3]["evidence_memory_id"], 11)    # 引文未命中 → 取最新记忆

    async def test_suggest_snapshots_batch_request_failure_returns_error_reason(self):
        with patch("memory_extractor.httpx.AsyncClient", lambda **_kw: _FakeCardClient({}, status=500)), \
             patch("memory_extractor.get_memory_api_key", return_value="key"), \
             patch("memory_extractor.get_memory_api_base_url", return_value="http://test"):
            payload = await memory_extractor.suggest_entity_snapshots_batch(
                [{"id": 1, "name": "x", "entity_type": "person", "aliases": [], "memories": []}]
            )
        self.assertIn("error", payload)
        self.assertIn("500", payload["error"])


if __name__ == "__main__":
    unittest.main()
