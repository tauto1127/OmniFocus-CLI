"""Loopback-only integration tests; all credentials and payloads are synthetic."""

import asyncio
import hashlib
import logging
import re
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient, _safe_redirect

USER, PASSWORD = "test-user", "test-password"


def md5(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


@pytest.fixture
def servers(tmp_path, monkeypatch):
    received = []
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(  # noqa: S603 - fixed command, temporary test paths only
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),  # noqa: S607
         "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    import omnifocus.sync.webdav as webdav

    original_init = webdav._PrivateAsyncClient.__init__

    def init_with_test_certificate(self, *args, **kwargs):
        kwargs["verify"] = False  # synthetic loopback server only
        kwargs["trust_env"] = False
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(webdav._PrivateAsyncClient, "__init__", init_with_test_certificate)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def serve(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            auth = self.headers.get("Authorization", "")
            received.append((self.server.server_port, self.command, self.path, auth, body))
            if self.path.startswith("/cross/"):
                self.send_response(307)
                self.send_header("Location", f"https://127.0.0.1:{other.server_port}/stolen")
            elif self.path.startswith("/loop/"):
                self.send_response(302)
                self.send_header("Location", self.path)
            elif self.path.startswith("/redir/"):
                self.send_response(302)
                self.send_header("Location", self.path.replace("/redir/", "/digest/", 1))
            elif self.path.startswith("/basic/"):
                if not auth.startswith("Basic "):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="test"')
                else:
                    self.send_response(200)
            elif self.path.startswith("/digest/"):
                parts = {}
                for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^, ]+))', auth):
                    parts[key] = quoted or bare
                valid = False
                if auth.startswith("Digest ") and all(
                    k in parts for k in ("username", "uri", "response", "nc", "cnonce", "qop")
                ):
                    ha1 = md5(f"{USER}:test-realm:{PASSWORD}")
                    ha2 = md5(f'{self.command}:{parts["uri"]}')
                    expected = md5(
                        f'{ha1}:test-nonce:{parts["nc"]}:{parts["cnonce"]}:{parts["qop"]}:{ha2}'
                    )
                    valid = (
                        parts["username"] == USER
                        and parts["uri"] == self.path
                        and parts["response"] == expected
                    )
                if valid:
                    self.send_response(200)
                else:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        'Digest realm="test-realm", nonce="test-nonce", algorithm=MD5, qop="auth"',
                    )
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = do_PROPFIND = do_PUT = serve

    main = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    other = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    for server in (main, other):
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (main, other)]
    for t in threads:
        t.start()
    yield f"https://127.0.0.1:{main.server_port}", other.server_port, received
    for s in (main, other):
        s.shutdown()
        s.server_close()
    for t in threads:
        t.join()


@pytest.mark.parametrize("method", ["GET", "PROPFIND", "PUT"])
def test_digest_and_same_origin_redirect_preserve_method_body(servers, method):
    base, _, received = servers

    async def run():
        async with WebDAVClient(base + "/redir/", USER, PASSWORD) as client:
            assert await client._request(method, base + "/redir/item", content=b"fixture") == b"ok"

    asyncio.run(run())
    assert [r[1] for r in received] == [method] * 3
    assert all(r[4] == b"fixture" for r in received)
    assert received[0][3] == "" and received[1][3] == ""
    assert received[2][3].startswith("Digest ")


def test_basic_challenge_fallback(servers):
    base, _, received = servers

    async def run():
        async with WebDAVClient(base + "/basic/", USER, PASSWORD) as client:
            await client.get_file("x")

    asyncio.run(run())
    assert len(received) == 2 and received[0][3] == "" and received[1][3].startswith("Basic ")


def test_cross_origin_rejected_without_request_or_credentials(servers):
    base, other_port, received = servers

    async def run():
        async with WebDAVClient(base + "/cross/", USER, PASSWORD) as client:
            with pytest.raises(OFWebDAVError, match="Unsafe"):
                await client.get_file("x")

    asyncio.run(run())
    assert all(r[0] != other_port for r in received)
    assert all(r[3] == "" for r in received)


def test_loop_is_bounded(servers):
    base, _, received = servers

    async def run():
        async with WebDAVClient(base + "/loop/", USER, PASSWORD) as client:
            with pytest.raises(OFWebDAVError, match="excessive"):
                await client.get_file("x")

    asyncio.run(run())
    assert len(received) == 6


def test_transport_debug_logs_do_not_include_path(servers, caplog):
    base, _, received = servers

    async def run():
        async with WebDAVClient(base + "/secret-account/", USER, PASSWORD) as client:
            await client.get_file("secret-filename")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert received[0][2] == "/secret-account/secret-filename"
    assert "secret-account" not in caplog.text
    assert "secret-filename" not in caplog.text


@pytest.mark.parametrize(
    ("src", "dst", "allowed"),
    [
        ("https://sync.omnigroup.com/a?b=1", "https://sync3.omnigroup.com/a?b=1", False),
        ("https://sync.omnigroup.com/a", "https://sync3.omnigroup.com/b", False),
        ("https://sync.omnigroup.com/a", "https://sync3.omnigroup.com.evil.example/a", False),
        ("https://sync.omnigroup.com/a", "http://sync3.omnigroup.com/a", False),
        ("https://sync.omnigroup.com/a", "https://user:pass@sync3.omnigroup.com/a", False),
        ("https://sync.omnigroup.com/a", "https://sync3.omnigroup.com:444/a", False),
        ("https://sync.omnigroup.com/a", "https://evil.example/a", False),
    ],
)
def test_official_backend_redirect_policy(src, dst, allowed):
    assert (
        _safe_redirect(httpx.URL(src), httpx.URL(dst), frozenset({"https://sync3.omnigroup.com"}))
        is allowed
    )
