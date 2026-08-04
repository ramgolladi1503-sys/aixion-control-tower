from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

JsonObject = dict[str, Any]
NotificationHandler = Callable[[str, JsonObject], Awaitable[None]]
RequestHandler = Callable[[str, JsonObject], Awaitable[Any]]


class JsonRpcError(RuntimeError):
    pass


class JsonRpcStdioClient:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
    ) -> None:
        self.process = process
        self.on_notification = on_notification
        self.on_request = on_request
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self.stderr_lines: list[str] = []

    async def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def _write(self, payload: JsonObject) -> None:
        if self.process.stdin is None or self.process.returncode is not None:
            raise JsonRpcError("JSON-RPC process input is unavailable.")
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()

    async def _read_loop(self) -> None:
        if self.process.stdout is None:
            return
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if "id" in message and ("result" in message or "error" in message):
                future = self._pending.get(message["id"])
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(JsonRpcError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
                continue
            if "id" in message and "method" in message:
                asyncio.create_task(self._handle_server_request(message))
                continue
            if "method" in message:
                asyncio.create_task(
                    self.on_notification(
                        str(message["method"]),
                        message.get("params") or {},
                    )
                )
        error = JsonRpcError("JSON-RPC process ended before pending requests completed.")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _handle_server_request(self, message: JsonObject) -> None:
        request_id = message["id"]
        try:
            result = await self.on_request(
                str(message["method"]),
                message.get("params") or {},
            )
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:  # noqa: BLE001 - translate adapter failure to JSON-RPC.
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(error)},
            }
        await self._write(response)

    async def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        while line := await self.process.stderr.readline():
            self.stderr_lines.append(line.decode("utf-8", errors="replace").rstrip("\n"))
            if len(self.stderr_lines) > 200:
                del self.stderr_lines[:50]

    async def close(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.gather(
            self._reader_task,
            self._stderr_task,
            return_exceptions=True,
        )
