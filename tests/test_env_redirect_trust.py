"""Environment-based opt-in stays narrow and never requires a real server."""

import httpx
import pytest

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("OF_WEBDAV_URL", "https://sync.omnigroup.com/a/")
    monkeypatch.setenv("OF_WEBDAV_USER", "synthetic")
    monkeypatch.setenv("OF_WEBDAV_PASS", "synthetic")
    monkeypatch.delenv("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", raising=False)


@pytest.mark.parametrize(
    "trust,allowed",
    [
        (None, False),
        ("", False),
        ("https://sync4.omnigroup.com", False),
        ("https://sync3.omnigroup.com, https://sync4.omnigroup.com", True),
    ],
)
async def test_redirect_from_env(monkeypatch, trust, allowed):
    if trust is not None:
        monkeypatch.setenv("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", trust)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "sync.omnigroup.com":
            return httpx.Response(307, headers={"location": "https://sync3.omnigroup.com/a/x"})
        return httpx.Response(200, content=b"ok")

    async with WebDAVClient.from_env() as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        if allowed:
            assert await client.get_file("x") == b"ok"
            assert seen == ["sync.omnigroup.com", "sync3.omnigroup.com"]
        else:
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.get_file("x")
            assert seen == ["sync.omnigroup.com"]


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://*.omnigroup.com",
        "http://sync3.omnigroup.com",
        "https://sync3.omnigroup.com:443",
        "https://sync3.omnigroup.com/",
        "https://sync3.omnigroup.com?x",
        "https://user@sync3.omnigroup.com",
        "https://sync3.omnigroup.com,",
        " ",
    ],
)
def test_invalid_trust_rejected(monkeypatch, value):
    monkeypatch.setenv("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", value)
    with pytest.raises(OFWebDAVError, match="Invalid OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS"):
        WebDAVClient.from_env()


@pytest.mark.parametrize(
    "value",
    [
        "https://test-bsdav-sgp.sync.omnigroup.com",
        "https://sync3.omnigroup.com, https://us-east.sync.omnigroup.com",
    ],
)
def test_regional_shard_trust_accepted(monkeypatch, value):
    monkeypatch.setenv("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", value)
    WebDAVClient.from_env()


@pytest.mark.parametrize(
    "value",
    [
        "https://.sync.omnigroup.com",
        "https://-bad.sync.omnigroup.com",
        "https://a.b.sync.omnigroup.com",
        "https://shard.sync.omnigroup.com.evil.example",
        "https://shard.sync-omnigroup.com",
    ],
)
def test_regional_shard_trust_rejects_lookalikes(monkeypatch, value):
    monkeypatch.setenv("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", value)
    with pytest.raises(OFWebDAVError, match="Invalid OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS"):
        WebDAVClient.from_env()
