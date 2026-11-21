"""Small aiohttp adapter for bounded, non-retrying POSTs and complete responses."""
import asyncio
import json

import aiohttp
import httpx

from history import packed


class Result:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    def __init__(self, *, base_url=None, timeout=40, connections=100, ssl=True, headers=None, cookies=None):
        self.session = aiohttp.ClientSession(
            base_url=base_url, headers=headers, cookies=cookies, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=aiohttp.TCPConnector(limit=connections, keepalive_timeout=2, ssl=ssl),
            json_serialize=packed)

    async def post(self, path, *, content=None, headers=None, json=None):
        try:
            # POST is not in aiohttp's retryable idempotent method set.
            async with self.session.post(path, data=content, headers=headers, json=json,
                                         allow_redirects=False) as response:
                return Result(response.status, await response.read())
        except asyncio.TimeoutError as error:
            raise httpx.ReadTimeout("完整HTTP响应超时") from error
        except aiohttp.ClientError as error:
            raise httpx.TransportError(f"{type(error).__name__}: {error}") from error

    async def aclose(self):
        await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
