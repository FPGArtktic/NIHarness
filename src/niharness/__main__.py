# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`python -m niharness`: the same entry point as the `nih` script.

A detached run re-invokes the harness this way, because the interpreter's path
is something the parent process can be certain of, where the location of a
console script depends on how the environment was installed.
"""

from niharness.cli.main import main

if __name__ == "__main__":
    main()
