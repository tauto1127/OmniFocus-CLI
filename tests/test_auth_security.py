import asyncio
import logging

import httpx
import pytest

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient, _challenges


def test_challenge_parser():
    headers = httpx.Headers(
        [
            (
                "www-authenticate",
                'orphan, Basic realm="a,b", Digest realm="x", nonce="n", qop="auth"',
            ),
            ("www-authenticate", 'Bearer realm="z"'),
        ]
    )
    assert _challenges(headers) == [
        'Basic realm="a,b"',
        'Digest realm="x", nonce="n", qop="auth"',
        'Bearer realm="z"',
    ]


@pytest.mark.parametrize(
    "challenge",
    [
        'Basic realm="x", Digest realm="x", nonce="n", qop="auth"',
        'Digest realm="x", nonce="n", qop="auth", Basic realm="x"',
    ],
)
def test_mixed_challenges_prefer_digest(challenge):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return (
            httpx.Response(200)
            if seen[-1]
            else httpx.Response(401, headers={"www-authenticate": challenge})
        )

    async def run():
        async with WebDAVClient("https://example.com/", "u", "p") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await client.get_file("x")

    asyncio.run(run())
    assert seen[0] is None and seen[1].startswith("Digest ")


def test_https_basic_fallback():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return (
            httpx.Response(200)
            if seen[-1]
            else httpx.Response(401, headers={"www-authenticate": 'Basic realm="x"'})
        )

    async def run():
        async with WebDAVClient("https://example.com/", "u", "p") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await client.get_file("x")

    asyncio.run(run())
    assert seen[0] is None and seen[1].startswith("Basic ")


@pytest.mark.parametrize(
    "base",
    [
        "https://example.com/?secret=123",
        "https://example.com/#secret",
        "https://user:pass@example.com/",
        "https://x:bad/",
    ],
)
def test_invalid_base_url(base):
    with pytest.raises(OFWebDAVError, match="Invalid WebDAV URL"):
        WebDAVClient(base, "u", "p")


@pytest.mark.parametrize("location", ["https://sync3.omnigroup.com/a/x", "/a/x?token=secret"])
def test_redirect_refused(location):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": location})

    async def run():
        async with WebDAVClient("https://sync.omnigroup.com/a/", "u", "p") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.put_file("x", b"private")

    asyncio.run(run())
    assert len(seen) == 1


def test_303_write_refused():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(303, headers={"location": "/other"})

    async def run():
        async with WebDAVClient("https://example.com/", "u", "p") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.put_file("x", b"private")

    asyncio.run(run())
    assert len(seen) == 1


def test_query_never_logged_or_sent(caplog):
    async def run():
        async with WebDAVClient("https://example.com/", "u", "p") as client:
            with pytest.raises(OFWebDAVError, match="Invalid") as exc:
                await client.get_file("x?token=secret")
            assert "secret" not in str(exc.value)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert "secret" not in caplog.text


def test_proxy_environment_remains_enabled(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    client = WebDAVClient("https://example.com/", "u", "p")
    try:
        assert client._client.trust_env is True
        assert any(mount is not None for mount in client._client._mounts.values())
    finally:
        asyncio.run(client.aclose())


def test_invalid_join_is_sanitized(monkeypatch):
    original_join = httpx.URL.join

    def fail_join(self, location):
        if isinstance(location, str):
            raise httpx.InvalidURL("secret")
        return original_join(self, location)

    monkeypatch.setattr(httpx.URL, "join", fail_join)

    async def run():
        async with WebDAVClient("https://example.com/", "u", "p") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(307, headers={"location": "/x"})
                )
            )
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.get_file("x")

    asyncio.run(run())
