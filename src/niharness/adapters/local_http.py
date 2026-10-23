# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""What every HTTP adapter in this harness shares.

Three adapters now dial a URL from configuration - inference, embeddings and
the vector store - and two rules apply to all three.

The first is the air gap: the harness runs with no network, so a configured
endpoint that is not on this host is a mistake caught at construction rather
than a request that leaves the machine. The second is that a failure must say
what actually happened - the server's own words when it refused
(:func:`checked`), and which request died when nothing answered
(:func:`explain`).

This lives beside the adapters rather than inside one of them because neither
rule is the inference adapter's property. It is deliberately not in the domain:
the domain may not import `urllib`, and a URL check without a URL parser is how
one ends up accepting `http://evil.com#@127.0.0.1`.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

import httpx

LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104
"""The hosts a backend may be served from. There is no flag to extend this."""


class AirGapViolationError(RuntimeError):
    """Raised when a configured URL points anywhere but this host."""


def require_local(base_url: str) -> str:
    """Return `base_url` unchanged, or refuse it.

    Args:
        base_url: the configured endpoint.

    Returns:
        The URL without a trailing slash.

    Raises:
        AirGapViolationError: when the host is not a loopback address. There is
            no configuration flag to switch this off.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip("[]")
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_HOSTS:
        msg = f"backend must be served from localhost, got {base_url!r}"
        raise AirGapViolationError(msg)
    return base_url.rstrip("/")


MAX_ERROR_BODY = 2_000
"""How much of a refusal's body is carried into the exception."""


def checked(response: httpx.Response) -> None:
    """Raise with the server's own explanation when a request was refused.

    `raise_for_status` reports the status and the URL, and throws away the
    body - which is where a server says *what* it objected to. A backend that
    refuses a request is not a mystery to be guessed at from the status code:
    the body goes into the message, truncated, so the operator reads the reason
    instead of rediscovering it with curl.

    Raises:
        httpx.HTTPStatusError: when the response is an error, with the body.
    """
    if not response.is_error:
        return
    body = response.text.strip()
    if len(body) > MAX_ERROR_BODY:
        body = f"{body[:MAX_ERROR_BODY]}... [truncated]"
    message = (
        f"{response.status_code} from {response.request.method} {response.request.url}: "
        f"{body or '(no body)'}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)


def explain(error: httpx.HTTPError, *, timeout_hint: str = "") -> str:
    """Return something worth reading about a failed HTTP request.

    Several httpx exceptions carry an empty message - a read timeout among
    them - so a report built from `str(error)` alone ends in a colon and
    nothing. `ConnectError` carries "All connection attempts failed", which
    names neither what was dialled nor what for. Both cases are a service that
    is not running or not answering, and both are worth one clear line.

    Args:
        error: what was raised.
        timeout_hint: what to suggest when the request timed out, for example
            which configuration key raises the limit.

    Returns:
        One line naming the request where there is one, and the reason.
    """
    detail = str(error).strip()
    if isinstance(error, httpx.TimeoutException) and not detail:
        detail = "no answer within the request timeout"
        if timeout_hint:
            detail = f"{detail}. {timeout_hint}"
    where = _requested(error)
    if not detail:
        detail = type(error).__name__
    return f"{where}: {detail}" if where else detail


def _requested(error: httpx.HTTPError) -> str:
    """Return "METHOD url" for the failed request, or empty when it is unknown.

    `HTTPError.request` raises rather than returning None when the exception
    was built without one, which is a surprise worth absorbing here.
    """
    try:
        request = error.request
    except RuntimeError:
        return ""
    return f"{request.method} {request.url}"
