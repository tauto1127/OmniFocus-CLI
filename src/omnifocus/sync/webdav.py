"""WebDAV client for OmniFocus sync server communication.

Provides async PROPFIND / GET / PUT operations against the WebDAV server
that hosts the ``.ofocus`` bundle.  All methods use exponential backoff with
up to three attempts on transient server errors (5xx).

Credentials are injected at construction time and must never be logged or
exposed in exception messages.

Usage::

    async with WebDAVClient.from_env() as client:
        files = await client.list_entries()
        data = await client.get_file(files[0])
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import asyncio
import logging
import os
import re
import time
from collections.abc import Generator
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
from httpx._client import BoundAsyncStream
from httpx._exceptions import request_context

from omnifocus.env import read_env_or_file
from omnifocus.errors import OFWebDAVError

log = logging.getLogger(__name__)

# WebDAV PROPFIND response namespace
_DAV_NS = "{DAV:}"

# Maximum number of retry attempts for transient 5xx errors
_MAX_RETRIES = 3

# Base delay in seconds between retries (doubles each attempt)
_RETRY_BASE_DELAY = 0.5
_MAX_REDIRECTS = 5


def _safe_redirect(
    source: httpx.URL, target: httpx.URL, trusted_origins: frozenset[str] = frozenset()
) -> bool:
    """Limit credential-bearing redirects to an origin or Omni's known backend pattern."""
    if target.username or target.password or target.fragment or target.query:
        return False
    if (source.scheme, source.host, source.port) == (target.scheme, target.host, target.port):
        return source.scheme in ("http", "https")
    return bool(
        source.scheme == target.scheme == "https"
        and source.port in (None, 443)
        and target.port in (None, 443)
        and str(target.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        in trusted_origins
        and source.host == "sync.omnigroup.com"
        and re.fullmatch(r"sync[0-9]+\.omnigroup\.com", target.host or "")
        and source.raw_path == target.raw_path
    )


def _challenges(headers: httpx.Headers) -> list[str]:
    """Split challenges at unquoted commas, not at Digest parameters."""
    result: list[str] = []
    for header in headers.get_list("www-authenticate"):
        parts = re.split(r',(?=(?:[^"\\]*(?:\\.|"(?:[^"\\]*\\.)*[^"\\]*"))*[^"\\]*$)', header)
        for part in parts:
            part = part.strip()
            if re.match(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+\s+(?!\s*=)", part):
                result.append(part)
            elif result:
                result[-1] += ", " + part
    return result


class _DigestAuth(httpx.DigestAuth):
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        flow = super().auth_flow(request)
        outgoing = next(flow)
        while True:
            response = yield outgoing
            if response.status_code == 401:
                digest = [
                    c for c in _challenges(response.headers) if re.match(r"^Digest\s", c, re.I)
                ]
                if digest:
                    response.headers["www-authenticate"] = digest[0]
            try:
                outgoing = flow.send(response)
            except StopIteration:
                return
            except (httpx.ProtocolError, KeyError, ValueError, NotImplementedError):
                raise OFWebDAVError("Unsupported or invalid WebDAV Digest challenge") from None


class _PrivateAsyncClient(httpx.AsyncClient):
    """HTTPX 0.28 single-request path without its URL-bearing INFO record."""

    async def _send_single_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transport_for_url(request.url)
        start = time.perf_counter()
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise RuntimeError("Expected an async request stream")
        with request_context(request=request):
            response = await transport.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.request = request
        response.stream = BoundAsyncStream(response.stream, response=response, start=start)
        self.cookies.extract_cookies(response)
        response.default_encoding = self._default_encoding
        return response


class WebDAVClient:
    """Async WebDAV client scoped to a single ``.ofocus`` bundle directory.

    Args:
        base_url: Full URL to the directory containing bundle files,
            e.g. ``https://dav.example.com/omnifocus/OmniFocus.ofocus/``.
            A trailing slash is required.
        username: WebDAV Basic Auth username.
        password: WebDAV Basic Auth password.  Never logged.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        *,
        trusted_redirect_origins: frozenset[str] = frozenset(),
    ) -> None:
        try:
            parsed_base = httpx.URL(base_url)
        except httpx.InvalidURL, ValueError:
            raise OFWebDAVError("Invalid WebDAV URL") from None
        if not base_url.endswith("/"):
            base_url += "/"
        if (
            parsed_base.scheme != "https"
            or not parsed_base.host
            or parsed_base.username
            or parsed_base.password
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise OFWebDAVError("Invalid WebDAV URL")
        self._base_url = base_url
        self._trusted_redirect_origins = trusted_redirect_origins
        self._username = username
        self._password = password
        self._client = _PrivateAsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> WebDAVClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> WebDAVClient:
        """Construct a client from environment variables.

        Credentials can be supplied in two ways (the second takes precedence):

        1. Embedded in the URL: ``https://user:pass@dav.example.com/path/``
        2. Separate variables: ``OF_WEBDAV_USER`` and ``OF_WEBDAV_PASS``

        Required variables:
            OF_WEBDAV_URL: Base URL, optionally with embedded credentials.
            OF_WEBDAV_USER: Username (overrides URL-embedded user).
            OF_WEBDAV_PASS: Password (overrides URL-embedded password).

        Optional OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS is a comma-separated list
        of exact HTTPS Omni backend origins. An empty or absent value trusts none.

        Raises:
            OFWebDAVError: If ``OF_WEBDAV_URL`` is missing, or if credentials
                cannot be found in either the URL or the separate variables.
        """
        raw_url = read_env_or_file("OF_WEBDAV_URL", error_type=OFWebDAVError) or ""
        if not raw_url:
            raise OFWebDAVError("Missing required environment variable: OF_WEBDAV_URL")

        # Parse credentials embedded in the URL (https://user:pass@host/path)
        parsed = urlsplit(raw_url)
        url_user = parsed.username or ""
        url_pass = parsed.password or ""

        # Strip credentials from the URL so they are not sent twice
        if url_user or url_pass:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            clean_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        else:
            clean_url = raw_url

        # Explicit env vars override URL-embedded credentials
        username = read_env_or_file("OF_WEBDAV_USER", error_type=OFWebDAVError) or url_user
        password = read_env_or_file("OF_WEBDAV_PASS", error_type=OFWebDAVError) or url_pass

        missing = []
        if not username:
            missing.append("OF_WEBDAV_USER")
        if not password:
            missing.append("OF_WEBDAV_PASS")
        if missing:
            raise OFWebDAVError(f"Missing required environment variables: {', '.join(missing)}")

        raw_trust = os.environ.get("OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS", "")
        origins = (
            frozenset(part.strip() for part in raw_trust.split(",")) if raw_trust else frozenset()
        )
        if any(
            not re.fullmatch(r"https://sync[0-9]+\.omnigroup\.com", origin) for origin in origins
        ):
            raise OFWebDAVError("Invalid OF_WEBDAV_TRUSTED_REDIRECT_ORIGINS")
        return cls(
            base_url=clean_url,
            username=username,
            password=password,
            trusted_redirect_origins=origins,
        )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def list_entries(self) -> list[str]:
        """List all files in the bundle directory via PROPFIND.

        Returns:
            List of relative filenames (for example ``["00000000=abc.zip", "20260322=x.client"]``),
            sorted lexicographically.

        Raises:
            OFWebDAVError: On HTTP error or parse failure.
        """
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "  <d:prop><d:displayname/><d:getcontenttype/></d:prop>"
            "</d:propfind>"
        )
        response = await self._request(
            "PROPFIND",
            self._base_url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode("utf-8"),
        )

        try:
            root = ET.fromstring(response)  # noqa: S314
        except ET.ParseError as exc:
            raise OFWebDAVError(f"PROPFIND response is not valid XML: {exc}") from exc

        filenames: list[str] = []
        for resp_el in root.iter(f"{_DAV_NS}response"):
            href_el = resp_el.find(f"{_DAV_NS}href")
            if href_el is None or href_el.text is None:
                continue
            href = href_el.text.rstrip("/")
            filename = href.rsplit("/", 1)[-1]
            if not filename or filename.endswith(".ofocus") or filename == "data":
                continue
            if "." in filename:
                filenames.append(filename)

        return sorted(filenames)

    async def list_bundle(self) -> list[str]:
        """List all bundle ZIPs in the bundle directory via PROPFIND."""
        return [filename for filename in await self.list_entries() if filename.endswith(".zip")]

    async def get_file(self, filename: str) -> bytes:
        """Download a file from the bundle directory.

        Args:
            filename: Relative filename, e.g. ``"00000000=abc.zip"``.

        Returns:
            Raw bytes of the file.

        Raises:
            OFWebDAVError: On HTTP error.
        """
        url = self._base_url + filename
        return await self._request("GET", url)

    async def put_file(self, filename: str, data: bytes) -> None:
        """Upload a file to the bundle directory.

        Args:
            filename: Relative filename for the uploaded bundle file.
            data: Raw bytes to upload.

        Raises:
            OFWebDAVError: On HTTP error.
        """
        url = self._base_url + filename
        content_type = "application/octet-stream"
        if filename.endswith(".zip"):
            content_type = "application/zip"
        elif filename.endswith(".client"):
            content_type = "application/xml"
        await self._request("PUT", url, headers={"Content-Type": content_type}, content=data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_following_redirects(
        self, method: str, url: str, headers: dict[str, str] | None, content: bytes | None
    ) -> httpx.Response:
        current = httpx.URL(url)
        if current.scheme != "https" or current.query or current.fragment:
            raise OFWebDAVError("Invalid WebDAV URL")
        for hop in range(_MAX_REDIRECTS + 1):
            # A new auth object for each destination prevents cached challenges
            # from being replayed on a different origin.
            response = await self._client.request(
                method,
                current,
                headers=headers or {},
                content=content,
                auth=_DigestAuth(self._username, self._password),
            )
            if response.status_code == 401:
                challenges = _challenges(response.headers)
                if (
                    any(re.match(r"^Basic\s", h, re.I) for h in challenges)
                    and not any(re.match(r"^Digest\s", h, re.I) for h in challenges)
                ):
                    response = await self._client.request(
                        method,
                        current,
                        headers=headers or {},
                        content=content,
                        auth=httpx.BasicAuth(self._username, self._password),
                    )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            if response.status_code == 303 and method.upper() not in ("GET", "HEAD"):
                raise OFWebDAVError("Unsafe WebDAV redirect refused")
            location = response.headers.get("location")
            if not location or hop == _MAX_REDIRECTS:
                raise OFWebDAVError("Invalid or excessive WebDAV redirects")
            try:
                target = current.join(location)
                allowed = _safe_redirect(current, target, self._trusted_redirect_origins)
            except httpx.InvalidURL, ValueError:
                raise OFWebDAVError("Unsafe WebDAV redirect refused") from None
            if not allowed:
                raise OFWebDAVError("Unsafe WebDAV redirect refused")
            # Do not rewrite PROPFIND/PUT to GET or drop their bodies.
            current = target
        raise OFWebDAVError("Excessive WebDAV redirects")  # pragma: no cover

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> bytes:
        """Execute an HTTP request with exponential-backoff retry on 5xx.

        Args:
            method: HTTP method string.
            url: Full URL.
            headers: Additional request headers.
            content: Request body bytes, or ``None``.

        Returns:
            Response body bytes.

        Raises:
            OFWebDAVError: After all retries are exhausted, or on 4xx errors.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            log.debug("%s (attempt %d/%d)", method, attempt + 1, _MAX_RETRIES)
            try:
                response = await self._send_following_redirects(method, url, headers, content)
            except httpx.RequestError as exc:
                log.debug("Request error (%s) — retrying", type(exc).__name__)
                last_exc = exc
                await asyncio.sleep(_RETRY_BASE_DELAY * (2**attempt))
                continue

            log.debug("← %d (%d bytes)", response.status_code, len(response.content))
            if response.status_code < 300:
                return response.content

            # 4xx errors are not retryable
            if 400 <= response.status_code < 500:
                raise OFWebDAVError(
                    f"{method} failed with status {response.status_code}",
                    status_code=response.status_code,
                )

            # 5xx: retry
            last_exc = OFWebDAVError(
                f"{method} failed with status {response.status_code}",
                status_code=response.status_code,
            )
            await asyncio.sleep(_RETRY_BASE_DELAY * (2**attempt))

        if isinstance(last_exc, OFWebDAVError):
            raise last_exc
        raise OFWebDAVError(f"{method} failed due to a request error") from None
