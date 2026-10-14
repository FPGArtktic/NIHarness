<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Third-party material

Vendored material keeps its own license file next to it and is listed here.
Models are never distributed with this repository; their licenses are recorded in
`docs/operations/models.md` and must be verified at download time.

| Path | What | Upstream | License |
|---|---|---|---|
| _(none vendored)_ | | | |

Runtime dependencies whose license matters most to this project, stated in the
ADR that introduced them:

| Dependency | What it is for | License | ADR |
|---|---|---|---|
| `mcp` | the Model Context Protocol client; tools run in servers it talks to | MIT | [0012](docs/adr/0012-mcp-is-the-tool-protocol.md) |

Planned, not yet vendored:

| Path | What | Upstream | License to verify |
|---|---|---|---|
| `benchmarks/suites/l3/swebench/` | SWE-bench subset for the L3 layer | princeton-nlp/SWE-bench | MIT |
| `benchmarks/suites/l1/bfcl/` | Berkeley Function Calling Leaderboard tasks | ShishirPatil/gorilla | Apache-2.0 |

Python dependency licenses are checked in the full gate with
`pip-licenses --allow-only-file licenses.toml`; Rust dependency licenses with
`cargo deny check licenses` in `tui/`.
