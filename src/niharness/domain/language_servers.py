# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which language server serves which language.

A second table beside the gate matrix, over the same :class:`Language` enum:
the gate says how a language is checked, this says how it is *understood*. Both
are policy, so both are data in the domain, and neither starts a process - that
is the tool server's business.

The point is token economics. `find_references` answers in fifteen lines what
`grep` answers in two hundred, and it answers correctly: a language server
knows a definition from a string that happens to match. For a 30B model that
difference is the difference between fixing the right call site and fixing a
comment.

Each language names *candidates* rather than one command, because the same
language is served by several implementations and an air-gapped image ships
whichever one it has. The first candidate whose command exists wins; a language
with no candidate installed is reported as absent rather than guessed at.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.gate import Language


class ServerCandidate(BaseModel):
    """One implementation that can serve a language."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()


class LanguageServer(BaseModel):
    """How one language is served, and which files belong to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: Language
    candidates: tuple[ServerCandidate, ...] = Field(min_length=1)
    extensions: tuple[str, ...] = Field(min_length=1)
    """Suffixes this server owns, lower-case and with the dot."""

    start_seconds: float = Field(
        default=20.0,
        gt=0,
        description="How long this server's handshake may take; a cold index is slow.",
    )


def _candidate(command: str, *args: str) -> ServerCandidate:
    """Build one candidate."""
    return ServerCandidate(command=command, args=tuple(args))


LANGUAGE_SERVERS: dict[Language, LanguageServer] = {
    Language.C_CPP: LanguageServer(
        language=Language.C_CPP,
        candidates=(
            _candidate("clangd", "--background-index", "--log=error"),
            _candidate("ccls"),
        ),
        extensions=(".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
        # clangd reads compile_commands.json and indexes in the background, so
        # its handshake is slower than a plain editor would suggest.
        start_seconds=45.0,
    ),
    Language.PYTHON: LanguageServer(
        language=Language.PYTHON,
        candidates=(
            _candidate("basedpyright-langserver", "--stdio"),
            _candidate("pyright-langserver", "--stdio"),
            _candidate("jedi-language-server"),
            _candidate("pylsp"),
        ),
        extensions=(".py", ".pyi"),
    ),
    Language.RUST: LanguageServer(
        language=Language.RUST,
        candidates=(_candidate("rust-analyzer"),),
        extensions=(".rs",),
        # cargo metadata plus a cold index: the slowest of the set.
        start_seconds=60.0,
    ),
    Language.GO: LanguageServer(
        language=Language.GO,
        candidates=(_candidate("gopls"),),
        extensions=(".go",),
    ),
    Language.BASH: LanguageServer(
        language=Language.BASH,
        candidates=(_candidate("bash-language-server", "start"),),
        extensions=(".sh", ".bash"),
    ),
    Language.HDL: LanguageServer(
        language=Language.HDL,
        candidates=(
            _candidate("verible-verilog-ls"),
            _candidate("svls"),
            _candidate("vhdl_ls"),
        ),
        extensions=(".v", ".sv", ".svh", ".vh", ".vhd", ".vhdl"),
    ),
    Language.YOCTO: LanguageServer(
        language=Language.YOCTO,
        candidates=(_candidate("bitbake-language-server"),),
        extensions=(".bb", ".bbappend", ".bbclass", ".inc"),
    ),
    Language.DOCS: LanguageServer(
        language=Language.DOCS,
        candidates=(_candidate("marksman", "server"),),
        extensions=(".md",),
    ),
}
"""The shipped table. `harness.toml` overrides a command under `[lsp.servers]`.

TCL is absent on purpose: there is no language server this project is willing
to name as a default. The gate still lints it, and `grep` still finds it.
"""


def server_of(language: Language) -> LanguageServer | None:
    """Return how `language` is served, or None when nothing serves it."""
    return LANGUAGE_SERVERS.get(language)


def language_of(path: str) -> Language | None:
    """Return the language that owns `path`, by suffix.

    Suffixes, not marker files: marker files say which toolchains a *worktree*
    has, which is the gate's question. Which server can answer about one file
    is this one.

    Args:
        path: a worktree-relative path.

    Returns:
        The owning language, or None when no server claims the suffix.
    """
    lowered = path.lower()
    for server in LANGUAGE_SERVERS.values():
        if any(lowered.endswith(suffix) for suffix in server.extensions):
            return server.language
    return None


def served_languages(detected: frozenset[Language]) -> tuple[Language, ...]:
    """Return the detected languages that have a server, in a stable order."""
    return tuple(
        language
        for language in sorted(LANGUAGE_SERVERS)
        if language in detected and language in LANGUAGE_SERVERS
    )
