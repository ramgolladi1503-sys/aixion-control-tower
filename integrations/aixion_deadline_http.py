"""Small HTTP/1.1 client with one absolute request deadline.

This module intentionally uses only the Python standard library. It does not
follow redirects and does not include URLs, headers, or response data in errors.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Mapping, cast
from urllib.parse import urlsplit

_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_LINE_BYTES = 8 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_DNS_WORKERS = 2
_DNS_QUEUE: queue.Queue[tuple[str, int, threading.Event, dict[str, object]]] = queue.Queue(
    maxsize=_DNS_WORKERS
)
_DNS_CAPACITY = threading.BoundedSemaphore(_DNS_WORKERS)
_DNS_INIT_LOCK = threading.Lock()
_DNS_STARTED = False


@dataclass(frozen=True)
class HttpResponse:
    """An HTTP response with normalized, lowercase header names."""

    status: int
    headers: Mapping[str, str]
    body: bytes


def request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
) -> HttpResponse:
    """Perform one HTTP/HTTPS request under a total monotonic deadline.

    The deadline includes bounded DNS lookup, connection establishment, TLS
    negotiation, status and headers, and every response body byte. DNS uses a
    fixed number of daemon workers. A resolver call that never returns occupies
    one permanent slot, and later DNS requests fail closed once all slots are
    occupied. This avoids an unbounded executor shutdown wait.
    """

    _validate_request(method, url, headers, body, timeout_seconds, max_response_bytes)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    deadline = time.monotonic() + timeout_seconds
    try:
        addresses = _resolve_addresses(parsed.hostname or "", port, deadline)
        return asyncio.run(
            _request_async(
                method,
                url,
                headers,
                body,
                timeout_seconds,
                max_response_bytes,
                addresses,
                deadline,
            )
        )
    except TimeoutError:
        raise TimeoutError("HTTP request deadline exceeded") from None
    except OSError:
        raise
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ssl.SSLError):
        raise OSError("HTTP transport failed") from None


def _validate_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
) -> None:
    if not isinstance(method, str) or not _TOKEN_RE.fullmatch(method):
        raise ValueError("invalid HTTP method")
    if not isinstance(url, str):
        raise ValueError("invalid HTTP URL")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("HTTP URL contains unsupported components")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("invalid HTTP URL port") from None
    if not isinstance(headers, Mapping):
        raise ValueError("headers must be a mapping")
    if body is not None and not isinstance(body, bytes):
        raise ValueError("body must be bytes or None")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
        raise ValueError("max_response_bytes must be a positive integer")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not _TOKEN_RE.fullmatch(name)
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
            or "\x00" in value
        ):
            raise ValueError("invalid HTTP header")
        if name.lower() in {"host", "connection", "content-length", "transfer-encoding"}:
            raise ValueError("host and framing headers are managed by the transport")


async def _request_async(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
    addresses: list[tuple[int, int, int, tuple]],
    absolute_deadline: float,
) -> HttpResponse:
    loop = asyncio.get_running_loop()
    deadline = min(loop.time() + timeout_seconds, absolute_deadline)
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if scheme == "https" else 80)
    ssl_context = ssl.create_default_context() if scheme == "https" else None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await _connect(addresses, host, ssl_context, deadline)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        host_header = _format_host(host, port, scheme)
        request_lines = [f"{method} {target} HTTP/1.1", f"Host: {host_header}"]
        for name, value in headers.items():
            try:
                name.encode("ascii")
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise ValueError("HTTP headers must use ASCII names and Latin-1 values") from None
            request_lines.append(f"{name}: {value}")
        if body is not None:
            request_lines.append(f"Content-Length: {len(body)}")
        request_lines.append("Connection: close")
        payload = ("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1")
        writer.write(payload)
        if body:
            writer.write(body)
        await _within_deadline(writer.drain(), deadline)

        status, response_headers, header_bytes = await _read_headers(reader, deadline)
        if header_bytes > _MAX_HEADER_BYTES:
            raise OSError("HTTP response headers exceed limit")
        response_body = await _read_body(
            reader,
            status,
            response_headers,
            deadline,
            max_response_bytes,
        )
        return HttpResponse(status=status, headers=response_headers, body=response_body)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ssl.SSLError):
        raise OSError("HTTP transport failed") from None
    finally:
        if writer is not None:
            writer.close()


def _format_host(host: str, port: int, scheme: str) -> str:
    shown_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return shown_host if port == default_port else f"{shown_host}:{port}"


def _resolve_addresses(host: str, port: int, deadline: float) -> list[tuple[int, int, int, tuple]]:
    """Resolve without asyncio's default executor or an unbounded task queue.

    A blocked OS resolver cannot be interrupted by Python. This pool therefore
    has a strict global worker cap and uses daemon threads. A timed-out lookup
    retains its capacity slot until getaddrinfo returns; when all slots are
    occupied, new hostname requests fail immediately instead of queuing.
    """
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return _lookup_addresses(host, port)

    if not _DNS_CAPACITY.acquire(blocking=False):
        raise OSError("DNS resolver capacity exhausted")
    _start_dns_workers()
    completed = threading.Event()
    result: dict[str, object] = {}
    try:
        _DNS_QUEUE.put_nowait((host, port, completed, result))
    except queue.Full:
        _DNS_CAPACITY.release()
        raise OSError("DNS resolver capacity exhausted") from None
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not completed.wait(remaining):
        raise TimeoutError("HTTP request deadline exceeded")
    if "error" in result:
        raise OSError("DNS lookup failed") from None
    return cast(list[tuple[int, int, int, tuple]], result["addresses"])


def _start_dns_workers() -> None:
    global _DNS_STARTED
    if _DNS_STARTED:
        return
    with _DNS_INIT_LOCK:
        if _DNS_STARTED:
            return
        for worker_number in range(_DNS_WORKERS):
            threading.Thread(
                target=_dns_worker,
                name=f"aixion-http-dns-{worker_number}",
                daemon=True,
            ).start()
        _DNS_STARTED = True


def _dns_worker() -> None:
    while True:
        host, port, completed, result = _DNS_QUEUE.get()
        try:
            result["addresses"] = _lookup_addresses(host, port)
        except BaseException as exc:
            result["error"] = exc
        finally:
            completed.set()
            _DNS_CAPACITY.release()
            _DNS_QUEUE.task_done()


def _lookup_addresses(host: str, port: int) -> list[tuple[int, int, int, tuple]]:
    """Small resolver seam used by deterministic deadline regression tests."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def _connect(
    addresses: list[tuple[int, int, int, tuple]],
    server_hostname: str,
    ssl_context: ssl.SSLContext | None,
    deadline: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error: OSError | None = None
    loop = asyncio.get_running_loop()
    for family, socktype, proto, _, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        try:
            await _within_deadline(loop.sock_connect(sock, sockaddr), deadline)
            return await _within_deadline(
                asyncio.open_connection(
                    sock=sock,
                    ssl=ssl_context,
                    server_hostname=server_hostname if ssl_context is not None else None,
                    limit=_MAX_HEADER_LINE_BYTES + 2,
                ),
                deadline,
            )
        except TimeoutError:
            sock.close()
            raise
        except OSError as exc:
            last_error = exc
            sock.close()
        except BaseException:
            sock.close()
            raise
    if last_error is not None:
        raise OSError("HTTP connection failed") from None
    raise OSError("DNS lookup returned no usable addresses")


async def _within_deadline(awaitable, deadline: float):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError("HTTP request deadline exceeded")
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError:
        raise TimeoutError("HTTP request deadline exceeded") from None


async def _readline(reader: asyncio.StreamReader, deadline: float, max_bytes: int) -> bytes:
    line = await _within_deadline(reader.readline(), deadline)
    if len(line) > max_bytes or not line.endswith(b"\n"):
        raise OSError("malformed or oversized HTTP response line")
    return line


async def _read_headers(
    reader: asyncio.StreamReader, deadline: float
) -> tuple[int, dict[str, str], int]:
    status_line = await _readline(reader, deadline, _MAX_HEADER_LINE_BYTES)
    try:
        parts = status_line[:-2].decode("ascii").split(" ", 2)
        if len(parts) < 2 or parts[0] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError
        status = int(parts[1])
        if not 100 <= status <= 599:
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        raise OSError("malformed HTTP status line") from None

    result: dict[str, str] = {}
    total = len(status_line)
    while True:
        line = await _readline(reader, deadline, _MAX_HEADER_LINE_BYTES)
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise OSError("HTTP response headers exceed limit")
        if line == b"\r\n":
            return status, result, total
        if line[:1] in {b" ", b"\t"} or b":" not in line:
            raise OSError("malformed HTTP response header")
        raw_name, raw_value = line[:-2].split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("latin-1").strip(" \t")
        except UnicodeDecodeError:
            raise OSError("malformed HTTP response header") from None
        if not _TOKEN_RE.fullmatch(name):
            raise OSError("malformed HTTP response header")
        if name in result:
            result[name] += ", " + value
        else:
            result[name] = value


async def _read_body(
    reader: asyncio.StreamReader,
    status: int,
    headers: Mapping[str, str],
    deadline: float,
    max_response_bytes: int,
) -> bytes:
    if status in {204, 304} or 100 <= status < 200:
        return b""
    transfer_encoding = headers.get("transfer-encoding", "")
    content_length = headers.get("content-length")
    if transfer_encoding:
        if transfer_encoding.lower().strip() != "chunked" or content_length is not None:
            raise OSError("unsupported or ambiguous HTTP body framing")
        return await _read_chunked(reader, deadline, max_response_bytes)
    if content_length is not None:
        if not re.fullmatch(r"[0-9]+", content_length.strip()):
            raise OSError("invalid HTTP content length")
        length = int(content_length)
        if length > max_response_bytes:
            raise OSError("HTTP response body exceeds limit")
        return await _read_exact(reader, length, deadline)
    return await _read_to_eof(reader, deadline, max_response_bytes)


async def _read_exact(reader: asyncio.StreamReader, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        amount = min(_READ_CHUNK_BYTES, size - len(chunks))
        chunk = await _within_deadline(reader.readexactly(amount), deadline)
        chunks.extend(chunk)
    return bytes(chunks)


async def _read_to_eof(
    reader: asyncio.StreamReader, deadline: float, max_response_bytes: int
) -> bytes:
    chunks = bytearray()
    while True:
        chunk = await _within_deadline(reader.read(min(_READ_CHUNK_BYTES, max_response_bytes + 1 - len(chunks))), deadline)
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > max_response_bytes:
            raise OSError("HTTP response body exceeds limit")


async def _read_chunked(
    reader: asyncio.StreamReader, deadline: float, max_response_bytes: int
) -> bytes:
    chunks = bytearray()
    trailer_bytes = 0
    while True:
        line = await _readline(reader, deadline, _MAX_HEADER_LINE_BYTES)
        size_text = line[:-2].split(b";", 1)[0]
        try:
            size_string = size_text.decode("ascii")
            if not _HEX_RE.fullmatch(size_string):
                raise ValueError
            size = int(size_string, 16)
        except (UnicodeDecodeError, ValueError):
            raise OSError("malformed HTTP chunk size") from None
        if size == 0:
            while True:
                trailer = await _readline(reader, deadline, _MAX_HEADER_LINE_BYTES)
                trailer_bytes += len(trailer)
                if trailer_bytes > _MAX_HEADER_BYTES:
                    raise OSError("HTTP response trailers exceed limit")
                if trailer == b"\r\n":
                    return bytes(chunks)
                if b":" not in trailer or trailer[:1] in {b" ", b"\t"}:
                    raise OSError("malformed HTTP response trailer")
        if size > max_response_bytes - len(chunks):
            raise OSError("HTTP response body exceeds limit")
        chunks.extend(await _read_exact(reader, size, deadline))
        if await _within_deadline(reader.readexactly(2), deadline) != b"\r\n":
            raise OSError("malformed HTTP chunk terminator")
