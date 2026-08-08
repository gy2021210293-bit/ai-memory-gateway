import unittest
from unittest.mock import AsyncMock, Mock, patch

import drives_integration


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DrivesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_linked_user_and_final_assistant_events(self):
        client = AsyncMock()
        client.post.side_effect = [Mock(status_code=200, text=""), Mock(status_code=200, text="")]

        with (
            patch.object(drives_integration, "DRIVESOID_URL", "https://drives.example"),
            patch.object(drives_integration.httpx, "AsyncClient", return_value=_AsyncClientContext(client)),
        ):
            await drives_integration.report_events(
                "用户消息",
                "这是完整的助手最终回复。",
                user_message_id="user-1",
                run_id="run-1",
            )

        self.assertEqual(client.post.await_count, 2)
        first = client.post.await_args_list[0].kwargs["json"]
        second = client.post.await_args_list[1].kwargs["json"]
        self.assertEqual(first, {
            "type": "msg_user",
            "payload": {"message_id": "user-1", "text": "用户消息"},
        })
        self.assertEqual(second, {
            "type": "msg_assistant",
            "payload": {
                "message_id": "run-1",
                "run_id": "run-1",
                "source_user_message_id": "user-1",
                "complete": True,
                "text": "这是完整的助手最终回复。",
            },
        })

    async def test_reports_only_user_event_when_no_final_assistant_text(self):
        client = AsyncMock()
        client.post.return_value = Mock(status_code=200, text="")

        with (
            patch.object(drives_integration, "DRIVESOID_URL", "https://drives.example"),
            patch.object(drives_integration.httpx, "AsyncClient", return_value=_AsyncClientContext(client)),
        ):
            await drives_integration.report_events(
                "用户消息",
                "",
                user_message_id="user-1",
                run_id="run-1",
            )

        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.post.await_args.kwargs["json"]["type"], "msg_user")
