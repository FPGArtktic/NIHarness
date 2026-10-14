<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

This project is built partly *by* agents working from written context. An agent
reads what is in the repository and nothing else; so does a contributor who
arrives a year later. Decisions that live only in a maintainer's head are
decisions that will be re-litigated, usually by accident, usually in the wrong
direction.

## Decision drivers

- An agent must be able to recover *why* a constraint exists before it tries to
  work around it.
- The reasoning behind a rejected option is as valuable as the chosen one.
- The record has to live next to the code and be rendered by the documentation
  build, or it will rot.

## Considered options

1. **MADR records in `docs/adr/`, rendered directly by MkDocs.**
2. A `DECISIONS.md` file that grows forever.
3. Commit messages only.
4. An external wiki.

## Decision outcome

Option 1. One Markdown file per decision in `docs/adr/`, numbered, in MADR
form, listed in `docs/adr/README.md` and in the documentation navigation.

A record is immutable once accepted. Changing a decision means writing a new
record that supersedes it, and marking the old one as superseded.

## Consequences

- Changing a port, adding a dependency or changing the pipeline shape requires
  an ADR in the same change. Pull requests without one are incomplete.
- Every dependency's license is stated in its ADR note, which is what makes the
  licence allowlist auditable.
- Commit messages stay about the change; ADRs stay about the decision.
- Some decisions will be recorded that later look obvious. That is the cheaper
  failure mode.
