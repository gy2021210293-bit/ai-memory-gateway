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
        examples = {
            "user": "user_traits_preferences",
            "self": "self_identity_commitment",
            "relationship": "relationship_practice_agreement",
        }
        for subject, cognitive_type in examples.items():
            item = database.normalize_cognitive_item_input({
                "subject": subject,
                "cognitive_type": cognitive_type,
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
                "subject": "entity", "cognitive_type": "user_traits_preferences", "content": "x",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "self", "cognitive_type": "invented", "content": "x",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "self", "cognitive_type": "user_recent_state", "content": "x",
            })

    def test_normalize_keeps_long_item_content(self):
        item = database.normalize_cognitive_item_input({
            "subject": "self", "cognitive_type": "self_identity_commitment", "content": "栖" * 300,
        })
        self.assertEqual(len(item["content"]), 300)

    def test_prompt_does_not_truncate_a_selected_item(self):
        content = "详细认知" * 80
        prompt = database.format_cognitive_items_for_prompt([{
            "subject": "user", "cognitive_type": "user_traits_preferences",
            "content": content, "status": "active",
        }])
        self.assertIn(content, prompt)

    def test_prompt_groups_the_three_cognitive_objects(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_traits_preferences", "content": "偏好简短回答", "status": "active"},
            {"subject": "self", "cognitive_type": "self_identity_commitment", "content": "重视诚实", "status": "active"},
            {"subject": "relationship", "cognitive_type": "relationship_practice_agreement", "content": "共同检查证据", "status": "active"},
        ])
        self.assertIn("对用户的认知", prompt)
        self.assertIn("AI 自我认知", prompt)
        self.assertIn("关系认知", prompt)
        self.assertIn("以当前用户消息为最高优先级", prompt)

    def test_prompt_has_per_subject_and_total_budgets(self):
        items = []
        for subject in ("user", "self", "relationship"):
            for index in range(6):
                items.append({
                    "subject": subject, "cognitive_type": {
                        "user": "user_traits_preferences", "self": "self_identity_commitment",
                        "relationship": "relationship_practice_agreement",
                    }[subject],
                    "content": f"{subject}-{index}-" + "认知" * 100, "status": "active",
                })
        prompt = database.format_cognitive_items_for_prompt(items)
        self.assertLessEqual(len(prompt), database.COGNITIVE_PROMPT_MAX_CHARS)
        for subject in ("user", "self", "relationship"):
            self.assertLessEqual(prompt.count(f"[{subject}-"), database.COGNITIVE_ITEMS_PER_SUBJECT)

    def test_prompt_keeps_only_one_item_per_cognitive_slot(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_recent_state", "content": "较早状态", "status": "active"},
            {"subject": "user", "cognitive_type": "user_recent_state", "content": "重复状态", "status": "active"},
        ])
        self.assertIn("较早状态", prompt)
        self.assertNotIn("重复状态", prompt)


if __name__ == "__main__":
    unittest.main()
