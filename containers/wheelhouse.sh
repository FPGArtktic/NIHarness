#!/bin/sh
# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build the wheelhouse the sandbox image installs from.
#
# This exists because the steps were a comment in the Containerfile that
# nobody had run, and when they were finally run, three of them were wrong: a
# command that does not exist in this uv, an export that pulled dev groups
# with no wheels for this interpreter, and a missing set of tools the gate
# needs. A script can be executed, and CI executes it.
#
# Run it on a connected machine. The image build itself needs no index.
set -eu

cd "$(dirname "$0")/.."
out=wheelhouse
rm -rf "$out"
mkdir -p "$out"

# The wheels have to match the interpreter *in the image*, not whichever one
# happens to be running this script. Debian trixie - the base in
# `Containerfile.sandbox` - ships Python 3.13, and packages with compiled
# extensions publish one wheel per version: `mypy`, `pydantic-core`,
# `cryptography`, `rpds-py`, `cffi`.
#
# Without these flags it works by coincidence. It did: this script downloaded
# `cp313` wheels on a machine whose `uv tool run` picked 3.13, and failed in CI
# where it picked something else - the image refusing `mypy` with "no matching
# distribution" from a wheelhouse that looked full.
#
# Keep in step with that base image. A mismatch is loud rather than subtle: the
# image build fails on the pip install, which is what CI runs.
target="--python-version 3.13 --implementation cp --abi cp313"
for tag in manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64; do
    target="$target --platform $tag"
done

# This project's own package. `uv build` fails loudly if the wheel cannot be
# assembled, which it could not for a while; a test now asserts the prompt
# layers are inside it.
uv build --wheel --out-dir "$out"

# Runtime dependencies only. The full lock carries this project's test and
# docs tooling, some of which has no wheel for this interpreter and none of
# which the sandbox needs.
uv export --frozen --no-dev --no-emit-project -o "$out/requirements.lock"
# $target is a deliberate list of flags, so it must stay unquoted.
# shellcheck disable=SC2086
uv tool run --from pip pip download \
    -r "$out/requirements.lock" -d "$out" --only-binary=:all: $target

# The tools the **gate** runs, because the gate runs inside the sandbox. An
# image with only the runtime builds without complaint and cannot lint a line
# of Python. `basedpyright` is the Python language server; it has to be
# installed rather than merely downloaded, since nothing depends on it.
# $target is a deliberate list of flags, so it must stay unquoted.
# shellcheck disable=SC2086
uv tool run --from pip pip download \
    ruff mypy pytest basedpyright -d "$out" --only-binary=:all: $target

printf '%s wheel(s) in %s, %s\n' \
    "$(find "$out" -name '*.whl' | wc -l)" "$out" "$(du -sh "$out" | cut -f1)"
