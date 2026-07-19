import logging
import sys
import types
import unittest


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


class CognitiveModelTests(unittest.TestCase):
    def test_normalize_accepts_all_three_subjects(self):
        for subject in ("user", "self", "relationship"):
            item = database.normalize_cognitive_item_input({
                "subject": subject,
                "cognitive_type": "stable_trait",
                "content": "  有证据的认知  ",
                "confidence": 0.8,
                "evidence_memory_ids": [3, "4", 3, "bad"],
            })
            self.assertEqual(item["subject"], subject)
            self.assertEqual(item["content"], "有证据的认知")
            self.assertEqual(item["evidence_memory_ids"], [3, 4])

    def test_normalize_rejects_unknown_subject_and_type(self):
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "entity", "cognitive_type": "stable_trait", "content": "x",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "self", "cognitive_type": "invented", "content": "x",
            })

    def test_normalize_limits_item_content(self):
        item = database.normalize_cognitive_item_input({
            "subject": "self", "cognitive_type": "identity_anchor", "content": "栖" * 300,
        })
        self.assertEqual(len(item["content"]), database.COGNITIVE_ITEM_MAX_CHARS)

    def test_prompt_keeps_hypothesis_uncertain(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "active_hypothesis", "content": "可能更喜欢简短回答", "status": "active"},
            {"subject": "self", "cognitive_type": "identity_anchor", "content": "重视诚实", "status": "active"},
            {"subject": "relationship", "cognitive_type": "stable_trait", "content": "共同检查证据", "status": "active"},
        ])
        self.assertIn("对用户的认知", prompt)
        self.assertIn("AI 自我认知", prompt)
        self.assertIn("关系认知", prompt)
        self.assertIn("（待确认）", prompt)
        self.assertIn("不能当作事实", prompt)

    def test_prompt_has_per_subject_and_total_budgets(self):
        items = []
        for subject in ("user", "self", "relationship"):
            for index in range(6):
                items.append({
                    "subject": subject, "cognitive_type": "stable_trait",
                    "content": f"{subject}-{index}-" + "认知" * 100, "status": "active",
                })
        prompt = database.format_cognitive_items_for_prompt(items)
        self.assertLessEqual(len(prompt), database.COGNITIVE_PROMPT_MAX_CHARS)
        for subject in ("user", "self", "relationship"):
            self.assertLessEqual(prompt.count(f"[{subject}-"), database.COGNITIVE_ITEMS_PER_SUBJECT)


if __name__ == "__main__":
    unittest.main()
