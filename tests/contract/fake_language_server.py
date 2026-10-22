# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""A language server that answers enough LSP to test the client, and no more.

It is a real process speaking the real framing over real pipes, because that is
where the mistakes are: the header parsing, the request correlation, and the
requests a server sends to its *client* - which this one does on purpose, since
a client that never answers them leaves a real server waiting for ever.

Every notification it receives is appended to the record file, so a test can
assert that a document was opened before it was asked about.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CONFIGURATION_ID = 9001
"""The id of the one request this server sends to its client."""


def write(message: dict[str, Any]) -> None:
    """Frame one message onto standard output."""
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def read() -> dict[str, Any] | None:
    """Read one framed message from standard input, or None at the end."""
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    body = sys.stdin.buffer.read(length)
    decoded: dict[str, Any] = json.loads(body)
    return decoded


def location(root: Path, line: int, character: int, length: int = 5) -> dict[str, Any]:
    """Return a Location in the fixture file."""
    return {
        "uri": (root / "src" / "app.py").as_uri(),
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + length},
        },
    }


def answer(message: dict[str, Any], root: Path, *, indexed: bool = True) -> Any:
    """Return the result for a request, by method."""
    method = message.get("method")
    if method == "initialize":
        return {
            "capabilities": {
                "referencesProvider": True,
                "documentSymbolProvider": True,
                "hoverProvider": True,
                "workspaceSymbolProvider": True,
            },
            "serverInfo": {"name": "fake-language-server"},
        }
    if method == "workspace/symbol":
        params = message.get("params") or {}
        if not indexed or params.get("query") != "build":
            # A cold index answers nothing, which is what clangd does on a
            # project it has not finished reading.
            return []
        return [
            {
                "name": "build",
                "kind": 12,
                "containerName": "app",
                "location": location(root, 3, 4),
            },
        ]
    if method == "textDocument/references":
        # Twice on the same line, as `f(1) + f(2)` would be.
        return [location(root, 8, 11), location(root, 8, 24)]
    if method == "textDocument/hover":
        return {
            "contents": {"kind": "markdown", "value": "```python\ndef build(name) -> str\n```"}
        }
    if method == "textDocument/documentSymbol":
        return [
            {
                "name": "build",
                "kind": 12,
                "range": {
                    "start": {"line": 3, "character": 0},
                    "end": {"line": 4, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 3, "character": 4},
                    "end": {"line": 3, "character": 9},
                },
                "children": [
                    {
                        "name": "name",
                        "kind": 13,
                        "range": {
                            "start": {"line": 3, "character": 10},
                            "end": {"line": 3, "character": 14},
                        },
                    },
                ],
            },
            {
                "name": "caller",
                "kind": 12,
                "range": {
                    "start": {"line": 7, "character": 0},
                    "end": {"line": 8, "character": 0},
                },
            },
        ]
    return None


def main() -> None:
    """Serve until `exit`, recording every notification."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--hang", action="store_true", help="never answer a request")
    parser.add_argument(
        "--no-index", action="store_true", help="workspace/symbol finds nothing"
    )
    parser.add_argument("--die", action="store_true", help="exit during the handshake")
    arguments = parser.parse_args()

    def record(entry: dict[str, Any]) -> None:
        with arguments.record.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    while True:
        message = read()
        if message is None:
            return
        method = message.get("method")
        identifier = message.get("id")
        if identifier is not None and method is None:
            # The client answered the question we asked it during startup.
            if message.get("id") == CONFIGURATION_ID:
                record({"answered": "workspace/configuration", "result": message.get("result")})
            continue
        if method == "exit":
            return
        if identifier is None:
            record({"notification": method, "params": message.get("params")})
            continue
        record({"request": method})
        if arguments.die:
            return
        if method == "initialize":
            # Ask the client something before answering, the way pyright does.
            write(
                {
                    "jsonrpc": "2.0",
                    "id": CONFIGURATION_ID,
                    "method": "workspace/configuration",
                    "params": {"items": [{"section": "python"}]},
                },
            )
        if arguments.hang and method != "initialize":
            continue
        write(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "result": answer(message, arguments.root, indexed=not arguments.no_index),
            },
        )


if __name__ == "__main__":
    main()
