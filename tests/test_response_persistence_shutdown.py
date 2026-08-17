import asyncio
import unittest

import main


class ResponsePersistenceShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_tasks = set(main._response_persistence_tasks)
        main._response_persistence_tasks.clear()

    async def asyncTearDown(self):
        tasks = tuple(main._response_persistence_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        main._response_persistence_tasks.clear()
        main._response_persistence_tasks.update(self.original_tasks)

    async def test_shutdown_waits_for_pending_response_commit(self):
        release = asyncio.Event()
        completed = asyncio.Event()

        async def commit():
            await release.wait()
            completed.set()

        task = asyncio.create_task(commit())
        main._response_persistence_tasks.add(task)
        drain = asyncio.create_task(
            main._drain_response_persistence_tasks(timeout_seconds=1)
        )
        await asyncio.sleep(0)
        self.assertFalse(drain.done())

        release.set()
        await drain
        self.assertTrue(completed.is_set())
        self.assertNotIn(task, main._response_persistence_tasks)

    async def test_shutdown_cancels_task_after_timeout(self):
        cancelled = asyncio.Event()

        async def commit():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(commit())
        main._response_persistence_tasks.add(task)
        await main._drain_response_persistence_tasks(timeout_seconds=0)

        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())
        self.assertNotIn(task, main._response_persistence_tasks)
