# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`rag_query`, served by the harness rather than by the sandbox.

Every other tool a model may call runs inside the sandbox container, and the
container is started `--network=none`. The vector store is a service on the
host. Those two facts cannot both hold for a tool that runs in the sandbox, and
of the two it is obvious which one gives way: the network flag is a
non-negotiable, so retrieval moves out of the container instead.

That is sound rather than merely convenient. `rag_query` reads no file, touches
no worktree and cannot write: it turns a string into chunks that were indexed
earlier, each with a citation. Nothing it does needs the worktree the sandbox
exists to confine, so hosting it in the harness process removes a capability
from the sandbox rather than adding one.

It is routed like any other tool. `CompositeToolProvider` sends the names this
provider serves here, the namespaced ones to their declared servers, and
everything else to the sandbox - so the domain still validates the call first,
and the call and its result are still events.
"""

from __future__ import annotations

from typing import Final

import structlog

from niharness.domain.messages import ToolSchema
from niharness.domain.rag import Hit, collections_for, merge_hits, render_hits
from niharness.domain.tools import (
    HARNESS_SERVED_TOOLS,
    AcceptedCall,
    RagQueryArgs,
    ToolId,
    ToolLimits,
    ToolName,
    ToolResult,
    truncate_result,
)
from niharness.ports.vector_store import (
    CollectionMissingError,
    VectorStore,
    VectorStoreError,
)

RAG_QUERY: Final = ToolId(ToolName.RAG_QUERY.value)

DESCRIPTION: Final = (
    "Search the indexed corpus by meaning and return the matching passages, each "
    "with a citation. Searches the code index, the ingested documents and this "
    "run's own context - which is where anything compacted out of the window "
    "went - unless `collection` names one. Use it to recover something you read "
    "earlier, or to find what a document says; it is not a substitute for "
    "`grep`, which finds exact text."
)

_log = structlog.get_logger(__name__)


class RetrievalTools:
    """A :class:`niharness.ports.tool_provider.ToolProvider` over the vector store."""

    def __init__(
        self,
        store: VectorStore,
        *,
        code_collection: str,
        doc_collection: str,
        run_collection: str = "",
        limits: ToolLimits | None = None,
    ) -> None:
        """Bind the tool to a store and the collections this run can see.

        Args:
            store: the index to search.
            code_collection: where the project's code is indexed.
            doc_collection: where `nih ingest` puts documents.
            run_collection: this run's own collection, holding compacted
                context. Empty when the run has none yet.
            limits: result caps; the shipped defaults when omitted.
        """
        self._store = store
        self._code = code_collection
        self._docs = doc_collection
        self._run = run_collection
        self._limits = limits or ToolLimits()

    @property
    def served(self) -> frozenset[ToolId]:
        """The tools this provider answers, for the router.

        Taken from the domain's set rather than restated, so the router's
        answer when this provider is absent cannot drift from what it serves
        when present.
        """
        return frozenset(ToolId(tool.value) for tool in HARNESS_SERVED_TOOLS)

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        """Return `rag_query`'s schema when the role may call it, nothing otherwise."""
        if RAG_QUERY not in allowlist:
            return ()
        return (
            ToolSchema(
                name=RAG_QUERY,
                description=DESCRIPTION,
                parameters=RagQueryArgs.model_json_schema(),
            ),
        )

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Run one query and return the hits, or say why there are none."""
        args = accepted.args
        if not isinstance(args, RagQueryArgs):  # pragma: no cover - validation types it
            return self._result(
                accepted, ok=False, text=f"{accepted.tool} got the wrong arguments"
            )
        searched = collections_for(
            args.collection,
            code=self._code,
            docs=self._docs,
            run=self._run,
        )
        groups: list[tuple[Hit, ...]] = []
        skipped: list[str] = []
        for collection in searched:
            try:
                groups.append(await self._store.query(collection, args.query, args.top_k))
            except CollectionMissingError:
                if args.collection:
                    return self._result(
                        accepted, ok=False, text=self._no_collection(collection)
                    )
                skipped.append(collection)
            except VectorStoreError as error:
                return self._result(accepted, ok=False, text=str(error))
        hits = merge_hits(groups, args.top_k)
        present = tuple(name for name in searched if name not in skipped)
        text = render_hits(hits, searched=present)
        if skipped:
            text = f"{text}\n\n(not indexed yet, so not searched: {', '.join(skipped)})"
        _log.info(
            "retrieval.query",
            collections=list(present),
            skipped=skipped,
            hits=len(hits),
        )
        return self._result(accepted, ok=True, text=text, hits=len(hits))

    def _no_collection(self, collection: str) -> str:
        """Explain a named collection that does not exist, and name the ones that do."""
        known = ", ".join(collections_for("", code=self._code, docs=self._docs, run=self._run))
        return (
            f"no collection {collection!r}. Leave `collection` out to search "
            f"everything this run can see ({known})."
        )

    def _result(
        self,
        accepted: AcceptedCall,
        *,
        ok: bool,
        text: str,
        hits: int | None = None,
    ) -> ToolResult:
        """Wrap text as a truncated tool result, with a one-line summary."""
        content, truncated = truncate_result(text, self._limits)
        summary = (
            f"{accepted.tool}: {hits} match(es)"
            if hits is not None
            else f"{accepted.tool}: {text.splitlines()[0] if text else 'no answer'}"
        )
        return ToolResult(
            call_id=accepted.call_id,
            tool=accepted.tool,
            ok=ok,
            content=content,
            truncated=truncated,
            summary=summary,
        )
