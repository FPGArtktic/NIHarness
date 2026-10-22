# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The table of which language server serves which language."""

from niharness.domain.gate import GATE_MATRIX, Language
from niharness.domain.language_servers import (
    LANGUAGE_SERVERS,
    ServerCandidate,
    language_of,
    served_languages,
    server_of,
)

UNSERVED: frozenset[Language] = frozenset({Language.TCL})
"""Languages the gate checks but no server here claims to understand."""


def test_every_language_the_gate_knows_is_either_served_or_deliberately_not() -> None:
    # A language that falls off both lists is the failure this catches: the
    # gate would check it and the model would have no way to read it.
    for language in GATE_MATRIX:
        assert (language in LANGUAGE_SERVERS) != (language in UNSERVED), language.value


def test_no_suffix_belongs_to_two_languages() -> None:
    seen: dict[str, Language] = {}
    for server in LANGUAGE_SERVERS.values():
        for suffix in server.extensions:
            assert suffix not in seen, f"{suffix} is claimed by {seen.get(suffix)} too"
            seen[suffix] = server.language
            assert suffix.startswith("."), suffix
            assert suffix == suffix.lower(), suffix


def test_a_file_is_owned_by_the_server_for_its_suffix() -> None:
    assert language_of("src/niharness/domain/tools.py") is Language.PYTHON
    assert language_of("tui/src/main.rs") is Language.RUST
    assert language_of("drivers/net.c") is Language.C_CPP
    assert language_of("drivers/net.CPP") is Language.C_CPP, "case does not decide"
    assert language_of("scripts/install.sh") is Language.BASH
    assert language_of("rtl/alu.sv") is Language.HDL
    assert language_of("recipes-core/thing_1.0.bb") is Language.YOCTO
    assert language_of("docs/index.md") is Language.DOCS


def test_a_suffix_nobody_serves_is_not_guessed_at() -> None:
    assert language_of("scripts/build.tcl") is None
    assert language_of("harness.toml") is None
    assert language_of("Makefile") is None


def test_every_language_names_at_least_one_command_and_one_suffix() -> None:
    for language, server in LANGUAGE_SERVERS.items():
        assert server.language is language
        assert server.candidates, language.value
        assert server.extensions, language.value
        assert server.start_seconds > 0


def test_only_the_detected_languages_are_served() -> None:
    detected = frozenset({Language.PYTHON, Language.TCL, Language.RUST})
    assert served_languages(detected) == (Language.PYTHON, Language.RUST)


def test_a_language_with_no_server_resolves_to_nothing() -> None:
    assert server_of(Language.TCL) is None
    assert server_of(Language.PYTHON) is not None


def test_the_candidates_are_ordered_by_preference() -> None:
    python = LANGUAGE_SERVERS[Language.PYTHON]
    first = python.candidates[0]
    assert first == ServerCandidate(command="basedpyright-langserver", args=("--stdio",))
    assert [candidate.command for candidate in python.candidates].index("pylsp") > 0
