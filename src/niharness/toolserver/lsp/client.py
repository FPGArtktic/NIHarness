# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""A minimal LSP client, over one language server's standard input and output.

LSP is JSON-RPC 2.0 with `Content-Length` framing and a lifecycle of its own:
`initialize`, then `initialized`, then requests, then `shutdown` and `exit`.
This is not a general-purpose client - it supports exactly what the harness's
three tools need, and it is deliberately small enough to read in one sitting.

Two details are easy to get wrong and both hang a real server:

* the server sends **requests** to the client, not only notifications -
  `workspace/configuration`, `client/registerCapability`,
  `window/workDoneProgress/create`. A client that never answers them leaves
  pyright waiting for its settings before it will index anything,
* every request needs a timeout. A language server that is still building its
  index is not broken, but a step's budget is not a place to find out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import structlog
from pydantic import JsonValue

HEADER_SEPARATOR = b"\r\n\r\n"
"""What ends an LSP message's headers."""

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
"""A single message larger than this is treated as a broken server."""

SHUTDOWN_GRACE = 2.0
"""Seconds a server gets to exit politely before it is killed."""

_log = structlog.get_logger(__name__)


class LspError(RuntimeError):
    """A language server could not be used."""


class LspUnavailableError(LspError):
    """The server did not start, or did not complete the handshake."""


class LspTimeoutError(LspError):
    """The server did not answer within the request timeout."""


class LspClient:
    """One language server process, spoken to over stdio."""

    def __init__(
        self,
        command: str,
        args: tuple[str, ...],
        root: Path,
        *,
        request_timeout: float = 20.0,
        start_timeout: float = 30.0,
    ) -> None:
        """Describe a server without starting it.

        Args:
            command: the executable.
            args: its arguments.
            root: the worktree, which becomes the server's workspace folder.
            request_timeout: how long any single request may take.
            start_timeout: how long the handshake may take.
        """
        self._command = command
        self._args = args
        self._root = root
        self._request_timeout = request_timeout
        self._start_timeout = start_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._diagnostics: dict[str, JsonValue] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()

    @property
    def command(self) -> str:
        """The executable this client speaks to."""
        return self._command

    @property
    def running(self) -> bool:
        """Whether the server process is still alive."""
        return self._process is not None and self._process.returncode is None

    def diagnostics_for(self, uri: str) -> JsonValue:
        """Return the last diagnostics the server published for `uri`."""
        return self._diagnostics.get(uri, [])

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the server and complete the handshake.

        Raises:
            LspUnavailableError: if the process cannot be started, dies during
                the handshake, or does not answer `initialize` in time.
        """
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                cwd=self._root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise LspUnavailableError(f"{self._command} did not start: {error}") from error
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._handshake(), self._start_timeout)
        except TimeoutError as error:
            await self.stop()
            raise LspUnavailableError(
                f"{self._command} did not finish the handshake in {self._start_timeout:g}s",
            ) from error
        except LspError:
            await self.stop()
            raise
        _log.info("lsp.started", command=self._command, root=str(self._root))

    async def _handshake(self) -> None:
        """Send `initialize`, then `initialized`."""
        await self.request("initialize", _initialize_params(self._root))
        await self.notify("initialized", {})

    async def stop(self) -> None:
        """Ask the server to exit, and make sure it does."""
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(LspError, OSError):
                await asyncio.wait_for(self.request("shutdown", None), SHUTDOWN_GRACE)
            with contextlib.suppress(OSError):
                await self.notify("exit", None)
            try:
                await asyncio.wait_for(process.wait(), SHUTDOWN_GRACE)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        self._fail_pending(LspUnavailableError(f"{self._command} stopped"))
        _log.info("lsp.stopped", command=self._command, returncode=process.returncode)

    # --------------------------------------------------------------- requests

    async def request(self, method: str, params: JsonValue) -> JsonValue:
        """Send a request and return its result.

        Args:
            method: the LSP method.
            params: its parameters.

        Returns:
            The `result` member of the response.

        Raises:
            LspTimeoutError: if the server does not answer in time.
            LspUnavailableError: if the server is not running.
            LspError: if the server answers with an error.
        """
        if not self.running:
            raise LspUnavailableError(f"{self._command} is not running")
        self._next_id += 1
        identifier = self._next_id
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        await self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, self._request_timeout)
        except TimeoutError as error:
            self._pending.pop(identifier, None)
            raise LspTimeoutError(
                f"{self._command} did not answer {method} in {self._request_timeout:g}s",
            ) from error

    async def notify(self, method: str, params: JsonValue) -> None:
        """Send a notification, which by definition has no answer."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        """Frame and write one message.

        Raises:
            LspUnavailableError: if the server's input is gone.
        """
        process = self._process
        if process is None or process.stdin is None:
            raise LspUnavailableError(f"{self._command} has no input stream")
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(
                    f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                )
                await process.stdin.drain()
            except (OSError, ConnectionResetError) as error:
                raise LspUnavailableError(
                    f"{self._command} closed its input: {error}"
                ) from error

    # ---------------------------------------------------------------- reading

    async def _read_loop(self) -> None:
        """Read messages until the server's output ends."""
        process = self._process
        if process is None or process.stdout is None:  # pragma: no cover - start sets both
            return
        try:
            while True:
                message = await _read_message(process.stdout)
                if message is None:
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:  # pragma: no cover - stop() cancels this task
            raise
        except (OSError, LspError) as error:
            _log.warning("lsp.read_failed", command=self._command, error=str(error))
        self._fail_pending(LspUnavailableError(f"{self._command} closed its output"))

    async def _dispatch(self, message: dict[str, JsonValue]) -> None:
        """Route one message: a response, a request from the server, or a notification."""
        identifier = message.get("id")
        method = message.get("method")
        if identifier is not None and method is None:
            self._settle(message, identifier)
            return
        if identifier is not None and isinstance(method, str):
            # The server is asking us something. What it asks hardly matters;
            # that it gets an answer does, because several servers block on it.
            await self._send({"jsonrpc": "2.0", "id": identifier, "result": _answer(method)})
            return
        if method == "textDocument/publishDiagnostics":
            params = message.get("params")
            if isinstance(params, dict) and isinstance(params.get("uri"), str):
                uri = params["uri"]
                if isinstance(uri, str):
                    self._diagnostics[uri] = params.get("diagnostics", [])

    def _settle(self, message: dict[str, JsonValue], identifier: JsonValue) -> None:
        """Complete the request `identifier` with the response's result or error."""
        if not isinstance(identifier, int):  # pragma: no cover - ids are ours
            return
        future = self._pending.pop(identifier, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(LspError(f"{self._command}: {error.get('message', error)}"))
            return
        future.set_result(message.get("result"))

    def _fail_pending(self, error: LspError) -> None:
        """Fail every outstanding request, so nothing waits on a dead server."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


async def _read_message(stream: asyncio.StreamReader) -> dict[str, JsonValue] | None:
    """Read one framed message, or None at the end of the stream.

    Raises:
        LspError: if the framing is unusable, which means the server is not
            speaking the protocol and nothing better will follow.
    """
    try:
        header = await stream.readuntil(HEADER_SEPARATOR)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    length = _content_length(header)
    if length is None or length > MAX_MESSAGE_BYTES:
        raise LspError(f"unusable framing: {header!r}")
    try:
        body = await stream.readexactly(length)
    except asyncio.IncompleteReadError:
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as error:
        raise LspError(f"the server sent something that is not JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise LspError("the server sent a JSON value that is not a message")
    return decoded


def _content_length(header: bytes) -> int | None:
    """Return the `Content-Length` of a header block, or None when there is none."""
    for line in header.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _answer(method: str) -> JsonValue:
    """Return the least surprising answer to a request from the server.

    `workspace/configuration` asks for one settings object per item and the
    honest answer is "no settings"; everything else is satisfied by a null,
    which is what an unregistered capability or a progress token amounts to.
    """
    if method == "workspace/configuration":
        return [{}]
    return None


def _initialize_params(root: Path) -> JsonValue:
    """Return the `initialize` parameters, declaring only what this client supports."""
    uri = root.as_uri()
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "niharness"},
        "rootUri": uri,
        "rootPath": str(root),
        "workspaceFolders": [{"uri": uri, "name": root.name}],
        "initializationOptions": {},
        "capabilities": {
            "workspace": {
                "configuration": True,
                "workspaceFolders": True,
                "symbol": {
                    "dynamicRegistration": False,
                    "symbolKind": {"valueSet": list(range(1, 27))},
                },
                "didChangeWatchedFiles": {"dynamicRegistration": False},
            },
            "textDocument": {
                "synchronization": {"dynamicRegistration": False, "didSave": True},
                "definition": {"linkSupport": True},
                "references": {"dynamicRegistration": False},
                "documentSymbol": {
                    "dynamicRegistration": False,
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "hover": {"contentFormat": ["plaintext", "markdown"]},
                "publishDiagnostics": {"relatedInformation": False},
            },
            "window": {"workDoneProgress": True},
        },
    }
