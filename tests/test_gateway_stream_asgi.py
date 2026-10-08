import unittest

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

import main


REAL_ASYNC_CLIENT = httpx.AsyncClient


class _Response:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        pass


class _UpstreamClient:
    response = None

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def build_request(self, *_args, **_kwargs):
        return object()

    async def send(self, _request, stream=False):
        return self.response


class GatewayStreamAsgiTests(unittest.IsolatedAsyncioTestCase):
    async def _read_response(self):
        app = FastAPI()

        @app.get("/stream")
        async def stream():
            return StreamingResponse(
                main.safe_stream_and_capture(
                    {}, {"stream": True}, "session", "", "model"
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        transport = httpx.ASGITransport(app=app)
        async with REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client:
            return await client.get("/stream")

    async def test_asgi_stream_delivers_normal_sse_and_headers(self):
        original_client = main.httpx.AsyncClient
        _UpstreamClient.response = _Response([
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
            b'data: [DONE]\n\n',
        ])
        main.httpx.AsyncClient = _UpstreamClient
        try:
            response = await self._read_response()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
            self.assertEqual(response.headers["cache-control"], "no-cache")
            self.assertIn('"content":"answer"', response.text)
            self.assertEqual(response.text.count("data: [DONE]"), 1)
        finally:
            main.httpx.AsyncClient = original_client

    async def test_asgi_stream_converts_unexpected_upstream_error_once(self):
        original_client = main.httpx.AsyncClient
        _UpstreamClient.response = _Response(
            [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
            RuntimeError("decoder failed"),
        )
        main.httpx.AsyncClient = _UpstreamClient
        try:
            response = await self._read_response()
            self.assertEqual(response.status_code, 200)
            self.assertIn('"content":"partial"', response.text)
            self.assertIn('"type": "upstream_stream_error"', response.text)
            self.assertEqual(response.text.count("data: [DONE]"), 1)
        finally:
            main.httpx.AsyncClient = original_client
