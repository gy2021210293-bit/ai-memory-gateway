"""定时后台任务设置与热更新测试。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import main


class ScheduledTasksSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    """测试 /api/settings 端点对定时后台任务的读取与保存。"""

    async def test_get_settings_contains_scheduled_task_fields(self):
        fake_db_cfg = {
            "COGNITIVE_AUTO_MODE": "auto",
            "COGNITIVE_AUTO_INTERVAL_HOURS": "8",
            "MEMORY_EVOLUTION_ENABLED": "false",
            "MEMORY_EVOLUTION_INTERVAL_HOURS": "16",
            "TRAIT_RECHECK_ENABLED": "false",
            "TRAIT_RECHECK_INTERVAL_HOURS": "48",
            "RELATION_RECHECK_ENABLED": "false",
            "RELATION_RECHECK_INTERVAL_HOURS": "72",
        }
        with patch.object(main, "get_all_gateway_config", AsyncMock(return_value=fake_db_cfg)):
            resp = await main.get_settings()
            self.assertEqual(resp.get("status"), "ok")
            s = resp["settings"]
            self.assertEqual(s["COGNITIVE_AUTO_MODE"], "auto")
            self.assertEqual(s["COGNITIVE_AUTO_INTERVAL_HOURS"], 8)
            self.assertFalse(s["MEMORY_EVOLUTION_ENABLED"])
            self.assertEqual(s["MEMORY_EVOLUTION_INTERVAL_HOURS"], 16)
            self.assertFalse(s["TRAIT_RECHECK_ENABLED"])
            self.assertEqual(s["TRAIT_RECHECK_INTERVAL_HOURS"], 48)
            self.assertFalse(s["RELATION_RECHECK_ENABLED"])
            self.assertEqual(s["RELATION_RECHECK_INTERVAL_HOURS"], 72)

    async def test_save_settings_updates_scheduled_task_fields_and_notifies(self):
        payload = {
            "MEMORY_EVOLUTION_ENABLED": False,
            "MEMORY_EVOLUTION_INTERVAL_HOURS": 6,
            "COGNITIVE_AUTO_MODE": "auto",
            "COGNITIVE_AUTO_INTERVAL_HOURS": 4,
            "TRAIT_RECHECK_ENABLED": False,
            "TRAIT_RECHECK_INTERVAL_HOURS": 12,
            "RELATION_RECHECK_ENABLED": False,
            "RELATION_RECHECK_INTERVAL_HOURS": 36,
        }
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=payload)

        mock_set_cfg = AsyncMock()
        mock_notify = MagicMock()

        with patch.object(main, "set_gateway_config", mock_set_cfg),              patch.object(main, "notify_scheduler_config_updated", mock_notify):
            resp = await main.save_settings(mock_request)
            self.assertEqual(resp.get("status"), "ok")
            self.assertIn("MEMORY_EVOLUTION_ENABLED", resp["updated"])
            self.assertIn("MEMORY_EVOLUTION_INTERVAL_HOURS", resp["updated"])
            self.assertIn("COGNITIVE_AUTO_MODE", resp["updated"])
            self.assertIn("COGNITIVE_AUTO_INTERVAL_HOURS", resp["updated"])
            self.assertIn("TRAIT_RECHECK_ENABLED", resp["updated"])
            self.assertIn("TRAIT_RECHECK_INTERVAL_HOURS", resp["updated"])
            self.assertIn("RELATION_RECHECK_ENABLED", resp["updated"])
            self.assertIn("RELATION_RECHECK_INTERVAL_HOURS", resp["updated"])

            # 验证全局变量已热更新
            self.assertFalse(main.MEMORY_EVOLUTION_ENABLED)
            self.assertEqual(main.MEMORY_EVOLUTION_INTERVAL_HOURS, 6)
            self.assertEqual(main.COGNITIVE_AUTO_MODE, "auto")
            self.assertEqual(main.COGNITIVE_AUTO_INTERVAL_HOURS, 4)
            self.assertFalse(main.TRAIT_RECHECK_ENABLED)
            self.assertEqual(main.TRAIT_RECHECK_INTERVAL_HOURS, 12)
            self.assertFalse(main.RELATION_RECHECK_ENABLED)
            self.assertEqual(main.RELATION_RECHECK_INTERVAL_HOURS, 36)

            # 验证调度器通知已触发
            mock_notify.assert_called_once()


class ScheduledTaskLoopsTests(unittest.IsolatedAsyncioTestCase):
    """测试后台循环对开关关闭和中断唤醒的响应。"""

    async def test_interruptible_sleep_wakes_on_notify(self):
        async def _trigger_wake():
            await asyncio.sleep(0.05)
            main.notify_scheduler_config_updated()

        wake_task = asyncio.create_task(_trigger_wake())
        start = asyncio.get_running_loop().time()
        # 即使设置了 100 秒休眠，通知到达后应立即在 0.5 秒内退出
        await main._interruptible_sleep(lambda: 100.0)
        elapsed = asyncio.get_running_loop().time() - start
        self.assertLess(elapsed, 1.0)
        await wake_task

    async def test_memory_evolution_loop_skips_when_disabled(self):
        call_count = 0

        async def _fake_once():
            nonlocal call_count
            call_count += 1

        with patch.object(main, "_memory_evolution_once", _fake_once),              patch.object(main, "_interruptible_sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            main.MEMORY_EVOLUTION_ENABLED = False
            main.MEMORY_ENABLED = True
            main.MEMORY_EXTRACT_ENABLED = True
            await main._memory_evolution_loop()
            self.assertEqual(call_count, 0)

    async def test_trait_requalify_loop_skips_when_disabled(self):
        call_count = 0

        async def _fake_once():
            nonlocal call_count
            call_count += 1

        with patch.object(main, "run_trait_requalify_once", _fake_once),              patch.object(main, "_interruptible_sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            main.TRAIT_RECHECK_ENABLED = False
            main.MEMORY_ENABLED = True
            main.MEMORY_EXTRACT_ENABLED = True
            await main._trait_requalify_loop()
            self.assertEqual(call_count, 0)

    async def test_entity_relation_discovery_loop_skips_when_disabled(self):
        call_count = 0

        async def _fake_once():
            nonlocal call_count
            call_count += 1

        with patch.object(main, "run_entity_relation_discovery_once", _fake_once),              patch.object(main, "_interruptible_sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            main.RELATION_RECHECK_ENABLED = False
            main.MEMORY_ENABLED = True
            main.MEMORY_EXTRACT_ENABLED = True
            await main._entity_relation_discovery_loop()
            self.assertEqual(call_count, 0)

    async def test_cognitive_auto_loop_skips_when_memory_disabled(self):
        call_count = 0

        async def _fake_once():
            nonlocal call_count
            call_count += 1

        with patch.object(main, "run_cognitive_auto_review_once", _fake_once),              patch.object(main, "_interruptible_sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            main.MEMORY_ENABLED = False
            await main._cognitive_auto_loop()
            self.assertEqual(call_count, 0)


class DashboardSettingsFrontendConsistencyTests(unittest.TestCase):
    """测试 HTML 与 JS 设置字段定义的一致性。"""

    def test_html_and_js_contain_all_scheduled_tasks_controls(self):
        with open("templates/dashboard.html", "r", encoding="utf-8") as f:
            html = f.read()
        with open("static/js/dashboard.js", "r", encoding="utf-8") as f:
            js = f.read()

        expected_ids = [
            "set-MEMORY_EVOLUTION_ENABLED",
            "set-MEMORY_EVOLUTION_INTERVAL_HOURS",
            "set-COGNITIVE_AUTO_MODE",
            "set-COGNITIVE_AUTO_INTERVAL_HOURS",
            "set-RELATION_RECHECK_ENABLED",
            "set-RELATION_RECHECK_INTERVAL_HOURS",
            "set-TRAIT_RECHECK_ENABLED",
            "set-TRAIT_RECHECK_INTERVAL_HOURS",
        ]
        for field_id in expected_ids:
            self.assertIn(field_id, html, f"HTML missing {field_id}")

        expected_keys = [
            "MEMORY_EVOLUTION_ENABLED",
            "MEMORY_EVOLUTION_INTERVAL_HOURS",
            "COGNITIVE_AUTO_MODE",
            "COGNITIVE_AUTO_INTERVAL_HOURS",
            "RELATION_RECHECK_ENABLED",
            "RELATION_RECHECK_INTERVAL_HOURS",
            "TRAIT_RECHECK_ENABLED",
            "TRAIT_RECHECK_INTERVAL_HOURS",
        ]
        for key in expected_keys:
            self.assertIn(f"'{key}'", js, f"JS _SETTINGS_FIELDS missing {key}")
