"""Loopback tests for the absolute-deadline HTTP transport."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable

import pytest

from integrations import aixion_deadline_http
from integrations.aixion_deadline_http import HttpResponse, request


class LoopbackServer:
    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}/test"
        self.error: BaseException | None = None

        def serve() -> None:
            try:
                conn, _ = self.listener.accept()
                with conn:
                    conn.settimeout(2)
                    _read_request(conn)
                    handler(conn)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as exc:  # surfaced by close() to avoid silent test thread errors
                self.error = exc
            finally:
                self.listener.close()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            self.listener.close()
            pytest.fail("loopback server thread did not stop")
        if self.error is not None:
            raise self.error


def _read_request(conn: socket.socket) -> None:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            return
        data.extend(chunk)


def _serve(response: bytes) -> LoopbackServer:
    return LoopbackServer(lambda conn: conn.sendall(response))


def _call(server: LoopbackServer, **kwargs) -> HttpResponse:
    return request(
        "GET",
        server.url,
        {},
        None,
        kwargs.pop("timeout_seconds", 1.0),
        kwargs.pop("max_response_bytes", 1024),
    )


def test_content_length_response_and_lowercase_headers() -> None:
    server = _serve(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-Trace: yes\r\n\r\nhello")
    try:
        response = _call(server)
    finally:
        server.close()
    assert response.status == 200
    assert response.headers["x-trace"] == "yes"
    assert response.body == b"hello"


def test_chunked_response_with_trailers() -> None:
    server = _serve(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"2;ext=x\r\nhi\r\n3\r\n!!!\r\n0\r\nX-End: yes\r\n\r\n"
    )
    try:
        response = _call(server)
    finally:
        server.close()
    assert response.body == b"hi!!!"


def test_eof_framed_response() -> None:
    server = _serve(b"HTTP/1.0 200 OK\r\nX-Test: eof\r\n\r\nbody")
    try:
        response = _call(server)
    finally:
        server.close()
    assert response.body == b"body"


def test_redirect_is_returned_without_following() -> None:
    server = _serve(b"HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 0\r\n\r\n")
    try:
        response = _call(server)
    finally:
        server.close()
    assert response.status == 302
    assert response.headers["location"] == "/elsewhere"


def test_content_length_over_limit_is_rejected() -> None:
    server = _serve(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata")
    try:
        with pytest.raises(OSError, match="exceeds limit"):
            _call(server, max_response_bytes=3)
    finally:
        server.close()


def test_eof_body_over_limit_is_rejected() -> None:
    server = _serve(b"HTTP/1.0 200 OK\r\n\r\ndata")
    try:
        with pytest.raises(OSError, match="exceeds limit"):
            _call(server, max_response_bytes=3)
    finally:
        server.close()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_slow_trickle_cannot_extend_absolute_deadline(phase: str) -> None:
    def slow_response(conn: socket.socket) -> None:
        if phase == "headers":
            # Keep trickling valid header bytes beyond the total deadline; a
            # short complete header can finish before the timeout under load.
            fragments = [b"HTTP/1.1 200 OK\r\n"] + [b"X-Pad: a\r\n"] * 20 + [b"\r\n", b"abc"]
        else:
            fragments = [b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n"] + [b"x"] * 12
        for fragment in fragments:
            try:
                conn.sendall(fragment)
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.035)

    server = LoopbackServer(slow_response)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            _call(server, timeout_seconds=0.12)
    finally:
        server.close()
    assert time.monotonic() - started < 0.6


def test_stalled_dns_returns_by_deadline_and_resolver_capacity_stays_bounded(monkeypatch) -> None:
    release = threading.Event()
    both_entered = threading.Event()
    entry_lock = threading.Lock()
    entries = 0

    def stalled_lookup(host: str, port: int) -> list[tuple[int, int, int, tuple]]:
        nonlocal entries
        with entry_lock:
            entries += 1
            if entries == aixion_deadline_http._DNS_WORKERS:
                both_entered.set()
        release.wait(timeout=2)
        return socket.getaddrinfo("127.0.0.1", port, type=socket.SOCK_STREAM)

    monkeypatch.setattr(aixion_deadline_http, "_lookup_addresses", stalled_lookup)
    arguments = ("GET", "http://stalled.example/test", {}, None, 0.15, 1024)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=aixion_deadline_http._DNS_WORKERS) as callers:
        futures = [callers.submit(request, *arguments) for _ in range(aixion_deadline_http._DNS_WORKERS)]
        try:
            assert both_entered.wait(timeout=1)
            for future in futures:
                with pytest.raises(TimeoutError, match="deadline"):
                    future.result(timeout=0.5)
            with pytest.raises(OSError, match="capacity exhausted"):
                request(*arguments)
        finally:
            release.set()
    assert time.monotonic() - started < 0.8
