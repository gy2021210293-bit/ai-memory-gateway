"""记忆演化：从原文记忆推断"没说但正确"的新内容（候选人工确认，layer=4 推断记忆）。"""
import unittest
from unittest.mock import AsyncMock, patch

import database
import main


def _fake_pool(conn):
    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *_args):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    return _Pool()


class MemoryEvolutionPipelineTests(unittest.IsolatedAsyncioTestCase):
    """_build_memory_derivation_draft 的校验链：前提、去重、相关性、防重组、跨时间。"""

    def _memories(self):
        return [
            {"id": 1, "content": "她坚持自己部署服务器", "importance": 8,
             "layer": 1, "created_at": None, "title": None},
            {"id": 2, "content": "她要求能导出全部数据", "importance": 7,
             "layer": 1, "created_at": None, "title": None},
            {"id": 3, "content": "她担心服务商倒闭数据丢失", "importance": 7,
             "layer": 1, "created_at": None, "title": None},
        ]

    def _candidate(self, **overrides):
        item = {
            "content": "她对数据所有权有高需求，倾向自部署而非云服务",
            "level": "inductive", "confidence": 0.75,
            "premise_memory_ids": [1, 2, 3], "reason": "多次提到自部署、导出、担心",
        }
        item.update(overrides)
        return item

    async def _run(self, memories, candidates, embeddings=None,
                   content_exists=False, gate_rows=None):
        patches = [
            patch.object(main, "get_verbatim_memories_for_derivation",
                         AsyncMock(return_value=memories)),
            patch.object(main._memory_extractor_module, "generate_memory_derivations",
                         AsyncMock(return_value=candidates)),
            patch.object(main, "memory_derivation_content_exists",
                         AsyncMock(return_value=content_exists)),
        ]
        if embeddings is not None:
            patches.append(patch.object(
                main, "compute_embeddings_batch", AsyncMock(return_value=embeddings)))

        class _GateConn:
            async def fetch(self, _query, *_args):
                return gate_rows or []

        if gate_rows is not None:
            patches.append(patch.object(main, "get_pool",
                                        return_value=_fake_pool(_GateConn())))
        for patcher in patches:
            patcher.start()
        try:
            return await main._build_memory_derivation_draft()
        finally:
            for patcher in patches:
                patcher.stop()

    async def test_valid_derivation_passes_checks(self):
        memories = self._memories()
        # 卡 [1,0] vs 前提 [0.5,0.5] → 余弦 0.707：相关（≥0.35）且非重组（<0.9）
        result = await self._run(
            memories, [self._candidate()],
            embeddings=[[1.0, 0.0], [0.5, 0.5], [0.5, 0.5], [0.5, 0.5]],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["premise_memory_ids"], [1, 2, 3])

    async def test_restatement_is_dropped(self):
        # 结论与某一前提几乎相同（余弦 ≥0.9）= 旧信息重组 → 丢弃
        result = await self._run(
            self._memories(), [self._candidate(premise_memory_ids=[1, 2])],
            embeddings=[[1.0, 0.0], [0.99, 0.01], [0.99, 0.01]],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])

    async def test_irrelevant_premise_is_dropped_and_candidate_falls_short(self):
        # 前提2 与结论无关（余弦 0）→ 被剔除 → 只剩 1 条前提 → 丢弃
        result = await self._run(
            self._memories(), [self._candidate(premise_memory_ids=[1, 2])],
            embeddings=[[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])

    async def test_duplicate_content_is_skipped(self):
        result = await self._run(
            self._memories(), [self._candidate()], content_exists=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])

    async def test_inductive_requires_cross_time_premises(self):
        # 归纳：前提只有 1 个日期 → 丢弃；演绎不要求跨时间
        memories = self._memories()
        inductive = self._candidate()
        deductive = self._candidate(level="deductive", content="她在意数据安全")
        gate_rows = [
            {"id": 1, "created_at": "2026-08-01T00:00:00+00:00"},
            {"id": 2, "created_at": "2026-08-01T00:00:00+00:00"},
            {"id": 3, "created_at": "2026-08-01T00:00:00+00:00"},
        ]
        result = await self._run(
            memories, [inductive, deductive], gate_rows=gate_rows,
        )
        self.assertTrue(result["ok"])
        contents = [it["content"] for it in result["items"]]
        self.assertEqual(contents, ["她在意数据安全"])  # 归纳被跨时间校验拦下


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class MemoryDerivationDbTests(unittest.IsolatedAsyncioTestCase):
    def _pending_row(self):
        return {
            "id": 7, "content": "她对数据所有权有高需求", "level": "inductive",
            "confidence": 0.75, "premise_memory_ids": [1, 2], "reason": "多次提到",
            "status": "pending",
        }

    async def test_accept_writes_layer4_derived_memory(self):
        class _Conn:
            def __init__(self):
                self.executions = []

            def transaction(self):
                return _FakeTx()

            async def fetchrow(self, query, *_args):
                q = " ".join(query.split())
                if "memory_derivation_pending" in q and "RETURNING" in q:
                    return self.__class__._ROW
                return None

            async def fetch(self, query, *_args):
                q = " ".join(query.split())
                if "FROM memories" in q:
                    return [{"id": 1, "importance": 8}, {"id": 2, "importance": 6}]
                return []

            async def fetchval(self, query, *_args):
                return 99

            async def execute(self, query, *args):
                self.executions.append((" ".join(query.split()), args))

        _Conn._ROW = {
            "id": 7, "content": "她对数据所有权有高需求", "level": "inductive",
            "confidence": 0.75, "premise_memory_ids": [1, 2], "reason": "多次提到",
        }
        conn = _Conn()
        with patch.object(database, "get_pool", return_value=_fake_pool(conn)):
            result = await database.accept_memory_derivation(7)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["memory_id"], 99)
        log = [
            (query, args) for query, args in conn.executions
            if query.startswith("INSERT INTO memory_derivation_log")
        ]
        self.assertIn("'accept'", log[0][0])
        self.assertEqual(log[0][1][0], 99)           # memory_id
        self.assertEqual(log[0][1][2], "inductive")  # level

    async def test_accept_fails_when_premises_missing(self):
        class _Conn:
            def transaction(self):
                return _FakeTx()

            async def fetchrow(self, query, *_args):
                q = " ".join(query.split())
                if "memory_derivation_pending" in q and "RETURNING" in q:
                    return {
                        "id": 7, "content": "结论", "level": "inductive",
                        "confidence": 0.7, "premise_memory_ids": [1, 2], "reason": "",
                    }
                return None

            async def fetch(self, _query, *_args):
                return [{"id": 1, "importance": 8}]  # 缺一条前提

        with patch.object(database, "get_pool", return_value=_fake_pool(_Conn())):
            result = await database.accept_memory_derivation(7)
        self.assertIn("error", result)
        self.assertIn("前提", result["error"])

    async def test_reject_records_decision(self):
        class _Conn:
            def __init__(self):
                self.executions = []

            def transaction(self):
                return _FakeTx()

            async def fetchrow(self, query, *_args):
                q = " ".join(query.split())
                if "memory_derivation_pending" in q and "RETURNING" in q:
                    return {
                        "content": "结论", "level": "inductive", "confidence": 0.7,
                        "premise_memory_ids": [1, 2], "reason": "原因",
                    }
                return None

            async def execute(self, query, *args):
                self.executions.append((" ".join(query.split()), args))

        conn = _Conn()
        with patch.object(database, "get_pool", return_value=_fake_pool(conn)):
            result = await database.reject_memory_derivation(7)
        self.assertEqual(result["status"], "ok")
        log = [
            (query, args) for query, args in conn.executions
            if query.startswith("INSERT INTO memory_derivation_log")
        ]
        self.assertIn("'reject'", log[0][0])
        self.assertEqual(log[0][1][0], "结论")  # content

    async def test_queue_inserts_pending_row(self):
        class _Conn:
            async def fetchrow(self, _query, *_args):
                return {"id": 5}

        with patch.object(database, "get_pool", return_value=_fake_pool(_Conn())):
            pending_id = await database.queue_memory_derivation({
                "content": "新推断", "level": "deductive", "confidence": 0.8,
                "premise_memory_ids": [1, 2], "reason": "前提蕴含",
            })
        self.assertEqual(pending_id, 5)


if __name__ == "__main__":
    unittest.main()
