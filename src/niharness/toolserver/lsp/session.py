# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The language servers a worktree can reach, and the three questions we ask.

One resident server per language, started the first time something needs it,
because a cold index costs tens of seconds and a step's budget is not the place
to discover that. The servers live as long as the tool server does, which is
the whole run - unlike a gate command, which gets a fresh container every time
(ADR 0008). An index is worth keeping; a build is not.

Everything is addressed **by name**. `workspace/symbol` turns a name into a
position, and the position is what `textDocument/references` and
`textDocument/hover` need. A tool that asked the model for a line and a column
would first have to make it read the file, which is the cost these tools exist
to remove.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse

import structlog
from pydantic import JsonValue

from niharness.domain.gate import Language
from niharness.domain.language_servers import (
    LANGUAGE_SERVERS,
    LanguageServer,
    ServerCandidate,
    language_of,
)
from niharness.toolserver.lsp.client import LspClient, LspError

SYMBOL_KINDS: dict[int, str] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum-member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type-parameter",
}
"""LSP `SymbolKind`, so an answer says `class` rather than `5`."""

LANGUAGE_IDS: dict[Language, str] = {
    Language.PYTHON: "python",
    Language.RUST: "rust",
    Language.GO: "go",
    Language.BASH: "shellscript",
    Language.HDL: "systemverilog",
    Language.YOCTO: "bitbake",
    Language.DOCS: "markdown",
}
"""The `languageId` a document is opened with; C and C++ are decided by suffix."""

CPP_SUFFIXES: frozenset[str] = frozenset({".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"})
"""Suffixes opened as `cpp`; the rest of the C family is opened as `c`."""

MAX_MATCHES = 10
"""How many definitions one answer carries before it says there are more."""

SYMBOL_ATTEMPTS = 3
"""How many times an empty symbol answer is retried.

A cold index answers nothing, and a name that does not exist answers nothing;
the protocol does not distinguish them. Retrying a few times catches an index
that is nearly ready, while waiting out a whole warm-up on every miss would
charge a model's typo the price of a cold start - minutes, over a run. The
handshake is where a cold start is paid for, once per language.
"""

RETRY_PAUSE = 0.5
"""Seconds between those attempts."""

MAX_SOURCE_BYTES = 2 * 1024 * 1024
"""A file larger than this is not opened; a language server would not thank us."""

SKIP_DIRECTORIES: frozenset[str] = frozenset(
    {".git", ".nih", "__pycache__", "node_modules", "target", "build", ".venv"},
)
"""Directories the fallback scan does not descend into."""

MAX_SCANNED_FILES = 2_000
"""How many files the fallback scan may look at before it gives up."""

MAX_OPENED_FILES = 8
"""How many files the fallback may open to ask a server about them."""

_log = structlog.get_logger(__name__)


class LanguageServers:
    """Lazily started language servers for one worktree."""

    def __init__(
        self,
        root: Path,
        *,
        table: Mapping[Language, LanguageServer] | None = None,
        overrides: Mapping[Language, ServerCandidate] | None = None,
        disabled: frozenset[Language] = frozenset(),
        which: Callable[[str], str | None] = shutil.which,
        request_timeout: float = 20.0,
        start_timeout: float = 30.0,
    ) -> None:
        """Bind to a worktree without starting anything.

        Args:
            root: the worktree; it becomes each server's workspace folder.
            table: which server serves which language; the shipped one by default.
            overrides: a command from `harness.toml`, winning over the candidates.
            disabled: languages this project does not want served.
            which: how an executable is looked up; a test substitutes it.
            request_timeout: how long one request may take.
            start_timeout: how long a handshake may take.
        """
        self._root = root
        self._table = dict(table if table is not None else LANGUAGE_SERVERS)
        self._overrides = dict(overrides or {})
        self._disabled = disabled
        self._which = which
        self._request_timeout = request_timeout
        self._start_timeout = start_timeout
        self._clients: dict[Language, LspClient | None] = {}
        self._opened: dict[Language, set[str]] = {}
        self._versions: dict[str, int] = {}
        self._locks: dict[Language, asyncio.Lock] = {}

    # ----------------------------------------------------------- availability

    def candidate_for(self, language: Language) -> ServerCandidate | None:
        """Return the command that would serve `language` on this host, if any."""
        if language in self._disabled:
            return None
        override = self._overrides.get(language)
        if override is not None:
            return override if self._which(override.command) else None
        server = self._table.get(language)
        if server is None:
            return None
        for candidate in server.candidates:
            if self._which(candidate.command):
                return candidate
        return None

    def installed(self, languages: Iterable[Language]) -> dict[Language, ServerCandidate]:
        """Return, for each of `languages`, the server that is actually installed."""
        found: dict[Language, ServerCandidate] = {}
        for language in languages:
            candidate = self.candidate_for(language)
            if candidate is not None:
                found[language] = candidate
        return found

    async def client_for(self, language: Language) -> LspClient | None:
        """Return a running client for `language`, starting one if needed.

        Returns:
            The client, or None when nothing serves this language here. A
            server that failed to start is remembered as absent, so a step does
            not pay the start timeout again on every call.
        """
        lock = self._locks.setdefault(language, asyncio.Lock())
        async with lock:
            if language in self._clients:
                existing = self._clients[language]
                if existing is None or existing.running:
                    return existing
            candidate = self.candidate_for(language)
            if candidate is None:
                self._clients[language] = None
                return None
            server = self._table.get(language)
            client = LspClient(
                candidate.command,
                candidate.args,
                self._root,
                request_timeout=self._request_timeout,
                # A cold index is paid for here, once per language, rather than
                # on every call: rust-analyzer and clangd need the room.
                start_timeout=max(
                    self._start_timeout,
                    server.start_seconds if server is not None else 0.0,
                ),
            )
            try:
                await client.start()
            except LspError as error:
                _log.warning("lsp.unavailable", language=language.value, error=str(error))
                self._clients[language] = None
                return None
            self._clients[language] = client
            self._opened[language] = set()
            return client

    async def aclose(self) -> None:
        """Stop every server that was started."""
        for client in self._clients.values():
            if client is not None:
                await client.stop()
        self._clients.clear()
        self._opened.clear()

    # -------------------------------------------------------------- documents

    def _language_id(self, relative: str) -> str:
        """Return the `languageId` a document of this path is opened with."""
        language = language_of(relative)
        if language is Language.C_CPP:
            return "cpp" if Path(relative).suffix.lower() in CPP_SUFFIXES else "c"
        return LANGUAGE_IDS.get(language, "plaintext") if language is not None else "plaintext"

    def _text(self, relative: str) -> str | None:
        """Return a worktree file's text, or None when it cannot be opened."""
        target = self._root / relative
        try:
            if target.stat().st_size > MAX_SOURCE_BYTES:
                return None
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    async def _open(self, client: LspClient, language: Language, relative: str) -> bool:
        """Open a document with its server, once.

        Most servers answer `textDocument/*` only about documents they have
        been given, so this is not an optimisation - without it the answer is
        an empty list and the reason is invisible.
        """
        opened = self._opened.setdefault(language, set())
        if relative in opened:
            return True
        text = self._text(relative)
        if text is None:
            return False
        await client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": (self._root / relative).as_uri(),
                    "languageId": self._language_id(relative),
                    "version": 1,
                    "text": text,
                },
            },
        )
        opened.add(relative)
        self._versions[relative] = 1
        return True

    async def changed(self, paths: Sequence[str]) -> None:
        """Tell every running server what just changed on disk.

        `apply_patch` is the only writer in the harness, so this is the one
        place where a server's view can be kept honest: the files it has open
        get a `didChange` with their new text, and everything else is announced
        as a watched-file change.
        """
        if not paths:
            return
        events: list[JsonValue] = [
            {"uri": (self._root / path).as_uri(), "type": 2}
            for path in paths
            if (self._root / path).exists()
        ]
        for language, client in self._clients.items():
            if client is None or not client.running:
                continue
            for path in paths:
                if path not in self._opened.get(language, set()):
                    continue
                text = self._text(path)
                if text is None:
                    continue
                version = self._versions.get(path, 1) + 1
                self._versions[path] = version
                await client.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {
                            "uri": (self._root / path).as_uri(),
                            "version": version,
                        },
                        "contentChanges": [{"text": text}],
                    },
                )
            if events:
                await client.notify("workspace/didChangeWatchedFiles", {"changes": events})

    # --------------------------------------------------------------- requests

    async def _symbols(self, client: LspClient, language: Language, name: str) -> list[Symbol]:
        """Return the symbols a server knows by that name.

        Two routes, because the first is better and not always available.
        `workspace/symbol` searches the server's index, which is what an editor
        uses - but an index is built in the background, and a cold one answers
        nothing at all. Measured against a real clangd on a one-file project,
        the index was still empty when the first question arrived.

        So when the index says nothing, the files that mention the name are
        found with a plain scan and handed to the server one at a time. The
        scan only decides *where to ask*; the answer is still the server's, so
        a name in a comment or a string is still not a definition.
        """
        for attempt in range(SYMBOL_ATTEMPTS):
            try:
                answer = await client.request("workspace/symbol", {"query": name})
            except LspError as error:
                _log.warning("lsp.symbol_failed", language=language.value, error=str(error))
                return []
            indexed = [
                symbol for symbol in _symbols_of(answer, self._root) if symbol.name == name
            ]
            if indexed:
                return indexed
            if attempt + 1 < SYMBOL_ATTEMPTS:
                await asyncio.sleep(RETRY_PAUSE)
        return await self._by_document(client, language, name)

    async def _by_document(
        self,
        client: LspClient,
        language: Language,
        name: str,
    ) -> list[Symbol]:
        """Ask the files that mention `name` what they define, until one does."""
        candidates = await asyncio.to_thread(self._mentioning, name, language)
        for relative in candidates[:MAX_OPENED_FILES]:
            if not await self._open(client, language, relative):
                continue
            try:
                answer = await client.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": (self._root / relative).as_uri()}},
                )
            except LspError:
                continue
            found = [
                symbol
                for symbol in _document_symbols(answer, relative, self._root)
                if symbol.name == name
            ]
            if found:
                _log.info("lsp.found_by_document", language=language.value, path=relative)
                return found
        return []

    def _mentioning(self, name: str, language: Language) -> list[str]:
        """Return this language's files that contain `name` as text.

        A substring scan, not a search for a definition: it decides which files
        are worth a question, and the server decides the answer.
        """
        server = self._table.get(language)
        if server is None:
            return []
        hits: list[str] = []
        scanned = 0
        for path in sorted(self._root.rglob("*")):
            if scanned >= MAX_SCANNED_FILES:
                break
            if path.suffix.lower() not in server.extensions or not path.is_file():
                continue
            if SKIP_DIRECTORIES & set(path.relative_to(self._root).parts):
                continue
            scanned += 1
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                if name in path.read_text(encoding="utf-8", errors="replace"):
                    hits.append(str(path.relative_to(self._root)))
            except OSError:
                continue
        return hits

    async def find_symbol(self, name: str, languages: Iterable[Language]) -> str:
        """Return where `name` is defined, with its signature where there is one."""
        lines: list[str] = []
        for language in languages:
            client = await self.client_for(language)
            if client is None:
                continue
            found = await self._symbols(client, language, name)
            for symbol in found[:MAX_MATCHES]:
                lines.append(
                    f"{symbol.where()}  {symbol.kind} {symbol.name}{symbol.container()}"
                )
                signature = await self._hover(client, language, symbol)
                if signature:
                    lines.append(f"    {signature}")
            if len(found) > MAX_MATCHES:
                lines.append(f"    ({len(found) - MAX_MATCHES} more in {language.value})")
        if not lines:
            return self._nothing(name, languages)
        return "\n".join(lines)

    async def _hover(self, client: LspClient, language: Language, symbol: Symbol) -> str:
        """Return the one-line signature a server shows on hover, if it shows one."""
        if symbol.line < 0 or not await self._open(client, language, symbol.path):
            return ""
        try:
            answer = await client.request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": (self._root / symbol.path).as_uri()},
                    "position": {"line": symbol.line, "character": symbol.column},
                },
            )
        except LspError:
            return ""
        return _hover_text(answer)

    async def find_references(
        self,
        name: str,
        languages: Iterable[Language],
        *,
        max_results: int = 30,
    ) -> str:
        """Return who uses `name`, as `path:line` with the line itself."""
        lines: list[str] = []
        total = 0
        for language in languages:
            client = await self.client_for(language)
            if client is None:
                continue
            found = await self._symbols(client, language, name)
            if not found:
                continue
            symbol = found[0]
            if not await self._open(client, language, symbol.path):
                continue
            try:
                answer = await client.request(
                    "textDocument/references",
                    {
                        "textDocument": {"uri": (self._root / symbol.path).as_uri()},
                        "position": {"line": symbol.line, "character": symbol.column},
                        "context": {"includeDeclaration": False},
                    },
                )
            except LspError as error:
                _log.warning("lsp.references_failed", language=language.value, error=str(error))
                continue
            # Two uses on one line are one line to read; a model pays for the
            # second copy and learns nothing from it.
            places = list(
                {place.where(): place for place in _locations_of(answer, self._root)}.values()
            )
            total += len(places)
            lines.append(f"{name} is defined at {symbol.where()} and used:")
            cache: dict[str, list[str]] = {}
            for place in places[:max_results]:
                lines.append(f"  {place.where()}  {self._source_line(place, cache)}")
        if not lines:
            return self._nothing(name, languages)
        if total > max_results:
            lines.append(f"  ({total - max_results} more; raise max_results to see them)")
        return "\n".join(lines)

    def _source_line(self, place: Symbol, cache: dict[str, list[str]]) -> str:
        """Return the source line a reference points at, stripped."""
        if place.path not in cache:
            text = self._text(place.path)
            cache[place.path] = text.splitlines() if text is not None else []
        body = cache[place.path]
        if 0 <= place.line < len(body):
            return body[place.line].strip()[:120]
        return ""

    async def outline(self, relative: str) -> str:
        """Return what is in a file, without the file.

        Raises:
            LspError: if no server owns the path's suffix, or the file cannot
                be opened. The caller turns that into a readable refusal.
        """
        language = language_of(relative)
        if language is None:
            raise LspError(
                f"no language server owns {Path(relative).suffix or 'this suffix'!r}"
            )
        client = await self.client_for(language)
        if client is None:
            raise LspError(f"no language server for {language.value} is installed here")
        if not await self._open(client, language, relative):
            raise LspError(f"{relative} could not be opened for {language.value}")
        answer = await client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": (self._root / relative).as_uri()}},
        )
        rendered = _outline_of(answer)
        return rendered or f"{relative}: the server reported no symbols"

    def _nothing(self, name: str, languages: Iterable[Language]) -> str:
        """Explain an empty answer without inventing a reason for it."""
        installed = self.installed(languages)
        if not installed:
            served = ", ".join(sorted(language.value for language in languages)) or "none"
            return (
                f"no language server is installed for the languages detected here ({served}); "
                "grep still works"
            )
        where = ", ".join(sorted(language.value for language in installed))
        return (
            f"no symbol named {name!r} in {where}. The name must match exactly, "
            "and an index that is still warming answers nothing; grep is the fallback."
        )


class Symbol:
    """A place in the worktree, as a language server reported it."""

    def __init__(
        self, name: str, kind: str, path: str, line: int, column: int, holder: str
    ) -> None:
        """Hold one resolved location. Lines and columns are LSP's, zero-based."""
        self.name = name
        self.kind = kind
        self.path = path
        self.line = line
        self.column = column
        self.holder = holder

    def where(self) -> str:
        """Return `path:line`, counting from one as every editor does."""
        return f"{self.path}:{self.line + 1}" if self.line >= 0 else self.path

    def container(self) -> str:
        """Return what holds this symbol, in brackets, or nothing."""
        return f" (in {self.holder})" if self.holder else ""


def _path_of(uri: str, root: Path) -> str:
    """Return a `file://` URI as a worktree-relative path."""
    target = Path(unquote(urlparse(uri).path))
    try:
        return str(target.relative_to(root))
    except ValueError:
        return str(target)


def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    """Return `value` as a list of JSON objects, ignoring anything else."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _text_of(value: JsonValue) -> str:
    """Return `value` when it is a string, otherwise nothing."""
    return value if isinstance(value, str) else ""


def _number_of(value: JsonValue, default: int = -1) -> int:
    """Return `value` when it is a whole number, otherwise `default`."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _start_of(value: JsonValue) -> tuple[int, int]:
    """Return the zero-based line and column a Range or a Location starts at.

    `workspace/symbol` nests its position under `location.range`, while a
    `DocumentSymbol` carries `range` directly. Both shapes land here, and
    reading one as the other is how an answer loses its line numbers.
    """
    if not isinstance(value, dict):
        return -1, 0
    span = value.get("range") if "range" in value else value
    if not isinstance(span, dict):
        return -1, 0
    start = span.get("start")
    if not isinstance(start, dict):
        return -1, 0
    return _number_of(start.get("line")), _number_of(start.get("character"), 0)


def _symbols_of(answer: JsonValue, root: Path) -> list[Symbol]:
    """Read a `workspace/symbol` answer, in either of the shapes it comes in."""
    symbols: list[Symbol] = []
    for item in _objects(answer):
        location = item.get("location")
        if not isinstance(location, dict):
            continue
        uri = _text_of(location.get("uri"))
        if not uri:
            continue
        line, column = _start_of(location)
        symbols.append(
            Symbol(
                name=_text_of(item.get("name")),
                kind=SYMBOL_KINDS.get(_number_of(item.get("kind")), "symbol"),
                path=_path_of(uri, root),
                line=line,
                column=column,
                holder=_text_of(item.get("containerName")),
            ),
        )
    return symbols


def _document_symbols(answer: JsonValue, relative: str, root: Path) -> list[Symbol]:
    """Flatten a `textDocument/documentSymbol` answer into locations.

    Both shapes appear: a `DocumentSymbol` tree, whose children matter because
    a method lives inside a class, and a flat `SymbolInformation` list.
    """
    found: list[Symbol] = []
    for item in _objects(answer):
        location = item.get("location")
        path = relative
        if isinstance(location, dict):
            uri = _text_of(location.get("uri"))
            if uri:
                path = _path_of(uri, root)
        span = item.get("selectionRange") or item.get("range") or location
        line, column = _start_of(span)
        found.append(
            Symbol(
                name=_text_of(item.get("name")),
                kind=SYMBOL_KINDS.get(_number_of(item.get("kind")), "symbol"),
                path=path,
                line=line,
                column=column,
                holder=_text_of(item.get("containerName")),
            ),
        )
        children = item.get("children")
        if children is not None:
            found.extend(_document_symbols(children, relative, root))
    return found


def _locations_of(answer: JsonValue, root: Path) -> list[Symbol]:
    """Read a `textDocument/references` answer."""
    places: list[Symbol] = []
    for item in _objects(answer):
        uri = _text_of(item.get("uri")) or _text_of(item.get("targetUri"))
        if not uri:
            continue
        line, column = _start_of(item)
        places.append(Symbol("", "", _path_of(uri, root), line, column, ""))
    return places


def _hover_text(answer: JsonValue) -> str:
    """Return the one line of a hover answer worth carrying.

    Servers disagree about what hover is: pyright returns a fenced signature,
    clangd returns a markdown document with a heading, a return type, a
    parameter list and the declaration. The line worth keeping is the one that
    looks like a signature, so a line with parentheses wins; failing that, the
    first line that says anything.
    """
    useful = [line for candidate in _hover_parts(answer) for line in _useful_lines(candidate)]
    for line in useful:
        if "(" in line and ")" in line:
            return line[:160]
    return useful[0][:160] if useful else ""


def _hover_parts(answer: JsonValue) -> list[str]:
    """Return the text of a hover answer, in any of the three shapes it has."""
    if not isinstance(answer, dict):
        return []
    contents = answer.get("contents")
    if isinstance(contents, str):
        return [contents]
    if isinstance(contents, dict):
        return [_text_of(contents.get("value"))]
    if isinstance(contents, list):
        return [
            part if isinstance(part, str) else _text_of(_objects(part)[0].get("value"))
            for part in contents
            if isinstance(part, str) or _objects(part)
        ]
    return []


def _useful_lines(text: str) -> list[str]:
    """Return a markdown block's lines without its scaffolding.

    A fence marker is not content, but what is inside a fence usually is: that
    is where a signature lives.
    """
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```") or set(stripped) <= {"-", "*", "_"}:
            continue
        kept.append(stripped.lstrip("#").strip().strip("`"))
    return [line for line in kept if line]


def _outline_of(answer: JsonValue, depth: int = 0) -> str:
    """Render a `textDocument/documentSymbol` answer, hierarchical or flat."""
    lines: list[str] = []
    for item in _objects(answer):
        kind = SYMBOL_KINDS.get(_number_of(item.get("kind")), "symbol")
        name = _text_of(item.get("name"))
        line, _ = _start_of(item.get("range") or item.get("location"))
        where = f"{line + 1:>5}" if line >= 0 else "    ?"
        lines.append(f"{where}  {'  ' * depth}{kind} {name}")
        children = item.get("children")
        if children is not None:
            nested = _outline_of(children, depth + 1)
            if nested:
                lines.append(nested)
    return "\n".join(lines)
