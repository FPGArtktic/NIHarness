# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identifiers and content digests.

Everything here is deterministic: the same inputs always produce the same
identifier. The domain never reads a clock or a random source; callers pass
timestamps and nonces in from the edge.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final, NewType

RunId = NewType("RunId", str)
"""Identifier of a run; also the name of its worktree branch suffix."""

StepId = NewType("StepId", str)
"""Identifier of a single step inside a run."""

CallId = NewType("CallId", str)
"""Identifier of one tool call, used to pair a call with its result."""

Digest = NewType("Digest", str)
"""Lowercase hex sha256, truncated to :data:`DIGEST_LENGTH` characters."""

DIGEST_LENGTH: Final = 16
"""Digest length in hex characters; 64 bits is ample for log correlation."""


def digest_bytes(payload: bytes) -> Digest:
    """Return the truncated sha256 of `payload`."""
    return Digest(hashlib.sha256(payload).hexdigest()[:DIGEST_LENGTH])


def digest_text(text: str) -> Digest:
    """Return the truncated sha256 of `text` encoded as UTF-8."""
    return digest_bytes(text.encode("utf-8"))


def digest_json(value: Any) -> Digest:  # noqa: ANN401 - any JSON-serialisable value
    """Return the digest of `value` serialised canonically.

    Keys are sorted and separators are fixed, so semantically equal payloads
    (for example two tool-call argument dicts) share a digest.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return digest_text(canonical)


def derive_id(*parts: str) -> str:
    """Derive a stable identifier from `parts`.

    Used for step and call identifiers so that replaying an event log
    reproduces the same ids.
    """
    return digest_text("\u001f".join(parts))


def run_branch(run_id: RunId) -> str:
    """Return the git branch a run works on."""
    return f"harness/run-{run_id}"
