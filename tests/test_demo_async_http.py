import asyncio
from pathlib import Path
import sys
import unittest

from aiohttp import web
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from async_http import Client


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_bytes_json_timeout_and_no_post_retries(self):
        received = []
        async def handler(request):
            received.append((request.path, await request.read(), request.cookies.get("test")))
            if request.path == "/disconnect":
                request.transport.close()
                return web.Response()
            if request.path == "/slow":
                await asyncio.sleep(.2)
            return web.json_response({"ok": True}, status=429 if request.path == "/limit" else 200)
        app = web.Application()
        app.router.add_post("/{path}", handler)
        server = web.AppRunner(app)
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        try:
            async with Client(base_url=base, timeout=.1, cookies={"test": "present"}) as client:
                raw = b'{"ratio": 0.0}'
                response = await client.post("/ok", content=raw)
                self.assertEqual(response.json(), {"ok": True})
                self.assertEqual(received[-1], ("/ok", raw, "present"))
                await client.post("/json", json={"ratio": 0.0})
                self.assertEqual(received[-1][1], b'{"ratio":0.0}')
                self.assertEqual((await client.post("/limit", content=raw)).status_code, 429)
                with self.assertRaises(httpx.TransportError):
                    await client.post("/disconnect", content=raw)
                with self.assertRaises(httpx.ReadTimeout):
                    await client.post("/slow", content=raw)
            self.assertEqual([p for p, _, _ in received], ["/ok", "/json", "/limit", "/disconnect", "/slow"])
        finally:
            await server.cleanup()
