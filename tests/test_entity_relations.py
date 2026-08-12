import logging
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch

import memory_extractor

# ---- 隔离 asyncpg / jieba，避免真实依赖 ----
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


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RelationPairNormTests(unittest.TestCase):
    def test_pair_norm_small_id_first(self):
        self.assertEqual(database._norm_pair_ids(5, 2), (2, 5))
        self.assertEqual(database._norm_pair_ids(2, 5), (2, 5))
        self.assertEqual(database._norm_pair_ids(7, 7), (7, 7))

    def test_pair_norm_accepts_strings(self):
        self.assertEqual(database._norm_pair_ids("9", "4"), (4, 9))


class RelationPromptTests(unittest.TestCase):
    def test_prompt_contains_names_and_json_format(self):
        pairs = [
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 3, "evidence": ["2026年7月陈皮去了北京", "陈皮在北京玩得很开心"]},
        ]
        prompt = memory_extractor.build_relation_description_prompt(pairs)
        self.assertIn("陈皮", prompt)
        self.assertIn("北京", prompt)
        self.assertIn("共享3条记忆", prompt)
        self.assertIn("只输出JSON数组", prompt)
        self.assertIn("namesake", prompt)
        self.assertIn("一句话描述或null", prompt)
        self.assertIn("警惕同名异物", prompt)

    def test_prompt_marks_substring_name_pairs_as_suspicious(self):
        pairs = [
            {"a_name": "肉肉", "a_type": "person", "b_name": "肉肉大米", "b_type": "place",
             "shared_total": 2, "evidence": []},
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 2, "evidence": []},
        ]
        prompt = memory_extractor.build_relation_description_prompt(pairs)
        self.assertIn("特别注意第 0 对", prompt)
        self.assertIn("「肉肉」", prompt)

    def test_prompt_without_suspicious_pairs_has_no_warning(self):
        pairs = [
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 2, "evidence": []},
            {"a_name": "张医生", "a_type": "person", "b_name": "陈皮", "b_type": "pet",
             "shared_total": 2, "evidence": []},
        ]
        prompt = memory_extractor.build_relation_description_prompt(pairs)
        self.assertNotIn("特别注意第", prompt)

    def test_prompt_shows_evidence_lines(self):
        pairs = [
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 2, "evidence": ["第一条共享记忆", "第二条共享记忆"]},
        ]
        prompt = memory_extractor.build_relation_description_prompt(pairs)
        self.assertIn("第一条共享记忆", prompt)
        self.assertIn("第二条共享记忆", prompt)


class DescribeEntityRelationsTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, side_effect):
        client = AsyncMock()
        client.post.side_effect = side_effect
        return client

    async def test_empty_pairs_returns_empty_dict(self):
        result = await memory_extractor.describe_entity_relations([])
        self.assertEqual(result, {})

    async def test_no_api_key_returns_none(self):
        with patch.object(memory_extractor, "get_memory_api_key", return_value=""):
            result = await memory_extractor.describe_entity_relations(
                [{"a_name": "A", "b_name": "B", "evidence": []}]
            )
        self.assertIsNone(result)

    async def test_parses_verdicts_and_filters_out_of_range(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": (
                '[{"pair":0,"verify":"ok","relation":"常去的地方"},'
                '{"pair":9,"verify":"ok","relation":"越界应被丢弃"}]'
            )}}]
        }
        client = AsyncMock()
        client.post.return_value = response
        pairs = [
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 3, "evidence": []},
        ]
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.describe_entity_relations(pairs)
        self.assertEqual(result[0]["verify"], "ok")
        self.assertEqual(result[0]["relation"], "常去的地方")
        self.assertNotIn(9, result)

    async def test_retries_when_reasoning_has_no_json_array(self):
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "先想想这些实体对，输出在最终 JSON 前被截断"}}]
        }
        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "choices": [{"message": {"content": '[{"pair":0,"verify":"ok","relation":"常去的地方"}]'}}]
        }
        client = AsyncMock()
        client.post.side_effect = [first, second]
        pairs = [
            {"a_name": "陈皮", "a_type": "pet", "b_name": "北京", "b_type": "place",
             "shared_total": 3, "evidence": []},
        ]
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.describe_entity_relations(pairs)
        self.assertEqual(result[0]["relation"], "常去的地方")
        self.assertEqual(client.post.await_count, 2)
        self.assertIn("只返回最终 JSON 数组", client.post.await_args_list[1].kwargs["json"]["messages"][-1]["content"])

    async def test_http_error_returns_none(self):
        response = Mock()
        response.status_code = 503
        response.text = "unavailable"
        client = AsyncMock()
        client.post.return_value = response
        pairs = [{"a_name": "A", "b_name": "B", "evidence": []}]
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            result = await memory_extractor.describe_entity_relations(pairs)
        self.assertIsNone(result)

    async def test_uses_relation_model_or_memory_model(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        client = AsyncMock()
        client.post.return_value = response
        pairs = [{"a_name": "A", "b_name": "B", "evidence": []}]
        with patch.object(memory_extractor, "get_memory_api_key", return_value="test-key"), patch.object(
            memory_extractor, "ENTITY_RELATION_MODEL", "relation-special"
        ), patch.object(
            memory_extractor.httpx, "AsyncClient", return_value=_AsyncClientContext(client)
        ):
            await memory_extractor.describe_entity_relations(pairs)
        self.assertEqual(client.post.await_args.kwargs["json"]["model"], "relation-special")


class RelationCandidateScanTests(unittest.IsolatedAsyncioTestCase):
    def _candidate_row(self, a_id, b_id, a_last=None, b_last=None, a_evidence=5, b_evidence=5):
        """Build a candidate-scan row; last_evidence None = never directly linked (treated recent)."""
        return {
            "a_id": a_id, "b_id": b_id, "shared_total": 3, "shared_events": 1, "shared_fragments": 2,
            "a_name": f"实体{a_id}", "a_type": "person", "a_evidence": a_evidence,
            "b_name": f"实体{b_id}", "b_type": "place", "b_evidence": b_evidence,
            "a_override": None, "b_override": None,
            "a_has_profile": False, "b_has_profile": False,
            "a_last_evidence": a_last, "b_last_evidence": b_last,
        }

    async def test_dormant_pair_is_filtered_out(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        conn = AsyncMock()
        conn.fetch.return_value = [
            self._candidate_row(1, 2, a_last=recent, b_last=recent),     # 活跃 → 保留
            self._candidate_row(3, 4, a_last=recent, b_last=old),        # 一端休眠 → 过滤
            self._candidate_row(5, 6, a_last=old, b_last=old),           # 两端休眠 → 过滤
        ]
        with patch.object(database, "get_pool", return_value=FakePool(conn)):
            result = await database.find_entity_relation_candidates(limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["a_id"], result[0]["b_id"]), (1, 2))

    async def test_never_linked_entities_treated_as_recent(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            self._candidate_row(1, 2, a_last=None, b_last=None),
        ]
        with patch.object(database, "get_pool", return_value=FakePool(conn)):
            result = await database.find_entity_relation_candidates(limit=10)
        self.assertEqual(len(result), 1)


class RelationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_normalizes_pair_order(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 1")
        with patch.object(database, "get_pool", return_value=FakePool(conn)):
            await database.upsert_entity_relation(9, 3, "常去的地方", 2)
        args = conn.execute.await_args.args  # (sql, a, b, relation, shared_count)
        self.assertEqual((args[1], args[2]), (3, 9))
        self.assertEqual(args[3], "常去的地方")
        self.assertEqual(args[4], 2)

    async def test_upsert_clamps_negative_shared_count(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 1")
        with patch.object(database, "get_pool", return_value=FakePool(conn)):
            await database.upsert_entity_relation(1, 2, "关系", -5)
        args = conn.execute.await_args.args
        self.assertEqual(args[4], 0)

    async def test_relations_of_entity_both_sides_with_lifecycle(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).isoformat()
        conn = AsyncMock()
        conn.fetch.return_value = [{
            "entity_id_a": 1, "entity_id_b": 2, "relation": "常去的地方", "shared_count": 3,
            "a_id": 1, "a_name": "陈皮", "a_type": "pet", "a_evidence": 5, "a_override": None,
            "b_id": 2, "b_name": "北京", "b_type": "place", "b_evidence": 0, "b_override": None,
            "a_last_evidence": recent, "b_last_evidence": None,
        }]
        with patch.object(database, "get_pool", return_value=FakePool(conn)):
            result = await database.relations_of_entity([1, 2])
        # 从实体1视角 → 关联端是2（candidate）
        self.assertEqual(result[1][0]["entity_id"], 2)
        self.assertEqual(result[1][0]["relation"], "常去的地方")
        self.assertEqual(result[1][0]["retrieval_status"], "candidate")
        # 从实体2视角 → 关联端是1（active，证据5且最近活跃）
        self.assertEqual(result[2][0]["entity_id"], 1)
        self.assertEqual(result[2][0]["retrieval_status"], "active")


if __name__ == "__main__":
    unittest.main()
