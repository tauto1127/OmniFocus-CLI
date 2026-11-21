import logging

import httpx
import pytest

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient, _PrivateAsyncClient


@pytest.mark.parametrize("method", ["GET", "PUT", "PROPFIND"])
async def test_plain_http_never_sent(method):
    with pytest.raises(OFWebDAVError, match="Invalid WebDAV URL"):
        WebDAVClient("http://example.test/", "user", "password")
    async with WebDAVClient("https://example.test/", "user", "password") as client:
        seen = []
        await client._client.aclose()
        def handler(request):
            seen.append(request)
            return httpx.Response(200)

        client._client = _PrivateAsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(OFWebDAVError, match="Invalid WebDAV URL"):
            await client._request(method, "http://example.test/private", content=b"private")
        assert seen == []


@pytest.mark.parametrize("challenge", [
    'Digest realm="private", nonce="n", algorithm=UNKNOWN',
    'Digest realm="private", nonce="n", qop="auth-int"',
    'Digest realm="private", nonce="n", qop="unexpected-private"',
    'Digest realm="private", nonce="n", broken',
    'Digest realm="private"',
    'Basic realm="x", Digest realm="private", nonce="n", algorithm=UNKNOWN',
])
async def test_bad_digest_fails_closed_without_leaking(challenge, caplog):
    seen = []
    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(401, headers={"www-authenticate": challenge})
    async with WebDAVClient("https://example.test/", "secret-user", "secret-pass") as client:
        await client._client.aclose()
        client._client = _PrivateAsyncClient(transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.DEBUG), pytest.raises(OFWebDAVError, match="Digest") as exc:
            await client.get_file("private-path")
    assert seen == [None]
    assert "private" not in str(exc.value)
    assert "secret-user" not in caplog.text and "secret-pass" not in caplog.text
    assert "private-path" not in caplog.text


async def test_httpx_info_does_not_log_sensitive_url(caplog):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"ok")
    async with WebDAVClient("https://example.test/private-account/", "u", "p") as client:
        await client._client.aclose()
        client._client = _PrivateAsyncClient(transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.DEBUG):
            assert await client.get_file("private-file.zip") == b"ok"
    assert seen == ["https://example.test/private-account/private-file.zip"]
    assert "private-account" not in caplog.text and "private-file" not in caplog.text


async def test_private_client_rejects_sync_stream():
    class SyncOnly(httpx.SyncByteStream):
        def __iter__(self):
            yield b"fixture"

    async with _PrivateAsyncClient() as client:
        request = httpx.Request("PUT", "https://example.test/", stream=SyncOnly())
        with pytest.raises(RuntimeError, match="async request stream"):
            await client._send_single_request(request)


async def test_digest_rejection_does_not_downgrade_to_basic():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(401, headers={"www-authenticate": (
            'Basic realm="x", Digest realm="x", nonce="n", qop="auth"'
        )})

    async with WebDAVClient("https://example.test/", "u", "p") as client:
        await client._client.aclose()
        client._client = _PrivateAsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(OFWebDAVError, match="401"):
            await client.get_file("x")
    assert seen[0] is None and seen[1].startswith("Digest ") and len(seen) == 2
