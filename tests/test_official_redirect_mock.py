"""No network: verify Omni-shaped cross-host routing and auth isolation."""

import asyncio

import httpx
import pytest

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient


def test_official_redirect_rechallenges_backend():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "sync.omnigroup.com":
            return httpx.Response(307, headers={"location": "https://sync3.omnigroup.com/a/x"})
        if not request.headers.get("authorization"):
            return httpx.Response(
                401, headers={"www-authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return httpx.Response(200, content=b"ok")

    async def run():
        async with WebDAVClient(
            "https://sync.omnigroup.com/a/",
            "test",
            "test",
            trusted_redirect_origins=frozenset({"https://sync3.omnigroup.com"}),
        ) as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            )
            assert await client.get_file("x") == b"ok"

    asyncio.run(run())
    assert seen[0] == ("sync.omnigroup.com", None)
    assert seen[1] == ("sync3.omnigroup.com", None)
    assert seen[2][0] == "sync3.omnigroup.com" and seen[2][1].startswith("Digest ")


def test_redirect_after_authenticated_request_does_not_leak():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if len(seen) == 1:
            return httpx.Response(
                401, headers={"www-authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return httpx.Response(307, headers={"location": "https://evil.example/collect"})

    async def run():
        async with WebDAVClient("https://sync.omnigroup.com/a/", "test", "test") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            )
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.get_file("x")

    asyncio.run(run())
    assert len(seen) == 2 and all(host == "sync.omnigroup.com" for host, _ in seen)
    assert seen[0][1] is None and seen[1][1].startswith("Digest ")
