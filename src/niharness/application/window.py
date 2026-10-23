# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The model's window: a view over the step, kept under its limit.

The domain decides *what* compaction drops; this decides nothing. It measures
the window with the backend's tokenizer, asks the policy for a plan, applies
it, and assembles the message list the protocol requires - a tool message
follows the assistant message that requested it, and conversation order is
chronological rather than by priority.

Measurement is the awkward part: the policy is pure and synchronous, the
tokenizer is a port and asynchronous. Everything the policy can ask to measure
is therefore measured up front - every item's content, and the digest line that
could replace it - and the policy is handed a lookup. A miss is an error rather
than a guess, because guessing a token count is exactly what this project
refuses to do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, ConfigDict, Field

from niharness.application.summarizer import Summarizer
from niharness.domain.artifacts import ContextSummary
from niharness.domain.context import (
    TURN_KINDS,
    CompactionPlan,
    CompactionPolicy,
    ContextItem,
    ItemKind,
    Priority,
    digest_line,
    plan_compaction,
)
from niharness.domain.identity import CallId, Digest, digest_text
from niharness.domain.messages import Message, MessageRole, ToolCallRequest
from niharness.domain.rag import context_chunks
from niharness.domain.tools import ToolResult
from niharness.ports.tokenizer import Tokenizer
from niharness.ports.vector_store import VectorStore, VectorStoreError

_log = structlog.get_logger(__name__)


class MeasurementMissingError(RuntimeError):
    """The policy asked for a token count that was not measured up front."""


class Entry(BaseModel):
    """One window position: what the policy reasons about, and what goes on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: ContextItem
    message: Message


@dataclass(frozen=True)
class ContextArchive:
    """Where compaction puts what it drops, so `rag_query` can hand it back.

    The event log keeps everything regardless - compaction is a view policy
    and never touches it - but the *model* cannot read the event log. Indexing
    what left the window is what makes the loss recoverable, which is the
    promise the context design makes and the reason the run's own collection
    is in retrieval's default search set.
    """

    store: VectorStore
    collection: str
    run_id: str


class WindowSnapshot(BaseModel):
    """The window as it will be sent, with what compaction did to get there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[Message, ...]
    tokens: int = Field(ge=0)
    fill: float = Field(ge=0.0, le=1.0)
    plan: CompactionPlan | None = None
    summary: ContextSummary | None = None
    archived: int = Field(
        default=0,
        ge=0,
        description="Chunks of dropped context written to the run's collection.",
    )

    @property
    def compacted(self) -> bool:
        """True when this snapshot is the result of a compaction pass."""
        return self.plan is not None


class Window:
    """Builds, measures and compacts one step's window."""

    def __init__(
        self,
        tokenizer: Tokenizer,
        policy: CompactionPolicy,
        *,
        summarizer: Summarizer | None = None,
        archive: ContextArchive | None = None,
        step: str = "",
    ) -> None:
        """Wire the tokenizer and the policy.

        Args:
            tokenizer: the backend's own tokenizer; the only source of counts.
            policy: the configured compaction policy.
            summarizer: produces a ContextSummary when the deterministic passes
                are not enough. Without one, folded-away turns are replaced by a
                pointer to the event log rather than summarised.
            archive: where to put what compaction drops. Without one the
                window still compacts and the content is still in the event
                log; it is only the model that cannot get it back.
            step: which step this window belongs to, for the citation on an
                archived chunk.
        """
        self._tokenizer = tokenizer
        self._policy = policy
        self._summarizer = summarizer
        self._archive = archive
        self._step = step
        self._entries: list[Entry] = []
        self._counts: dict[str, int] = {}
        self._next_seq = 0

    @property
    def entries(self) -> tuple[Entry, ...]:
        """The current window, in order."""
        return tuple(self._entries)

    async def _measure(self, text: str) -> int:
        """Return the token count of `text`, measured once and remembered."""
        key = str(digest_text(text))
        if key not in self._counts:
            self._counts[key] = await self._tokenizer.count_text(text)
        return self._counts[key]

    def _lookup(self, text: str) -> int:
        """Return a pre-measured token count.

        Raises:
            MeasurementMissingError: if the text was never measured. The policy
                only rewrites items into digest lines, and those are measured
                before planning, so a miss means the policy changed and this
                code did not.
        """
        key = str(digest_text(text))
        if key not in self._counts:
            msg = f"token count for {text[:40]!r} was never measured"
            raise MeasurementMissingError(msg)
        return self._counts[key]

    async def _add(
        self,
        *,
        kind: ItemKind,
        priority: Priority,
        content: str,
        message: Message,
        tool: str | None = None,
        target: str = "",
        lines: int | None = None,
    ) -> None:
        """Measure an item and append it to the window."""
        self._next_seq += 1
        tokens = await self._measure(content)
        item = ContextItem(
            id=f"{kind.value}-{self._next_seq}",
            priority=priority,
            kind=kind,
            content=content,
            tokens=tokens,
            digest=digest_text(content),
            origin_seq=self._next_seq,
            tool=tool,
            target=target,
            lines=lines,
        )
        self._entries.append(Entry(item=item, message=message))

    async def add_prompt(self, text: str) -> None:
        """Add a prompt layer. Never dropped, never rewritten."""
        await self._add(
            kind=ItemKind.PROMPT_LAYER,
            priority=Priority.PROMPT,
            content=text,
            message=Message(role=MessageRole.SYSTEM, content=text),
        )

    async def add_task(self, text: str) -> None:
        """Add the current task from the plan. Never dropped."""
        await self._add(
            kind=ItemKind.TASK,
            priority=Priority.PROMPT,
            content=text,
            message=Message(role=MessageRole.USER, content=text),
        )

    async def add_notes(self, text: str) -> None:
        """Add the NOTES.md pointer or an open reviewer note. Never dropped."""
        await self._add(
            kind=ItemKind.NOTES,
            priority=Priority.NOTES,
            content=text,
            message=Message(role=MessageRole.USER, content=text),
        )

    async def add_instruction(self, text: str) -> None:
        """Add a harness instruction: a rejection, a reflection, a finalisation."""
        await self._add(
            kind=ItemKind.INSTRUCTION,
            priority=Priority.CURRENT_TURNS,
            content=text,
            message=Message(role=MessageRole.USER, content=text),
        )

    async def add_assistant(
        self,
        content: str,
        tool_calls: Sequence[ToolCallRequest] = (),
    ) -> None:
        """Add an assistant turn, with the calls it proposed. Reasoning is not stored."""
        await self._add(
            kind=ItemKind.TURN,
            priority=Priority.CURRENT_TURNS,
            content=content,
            message=Message(
                role=MessageRole.ASSISTANT,
                content=content,
                tool_calls=tuple(tool_calls),
            ),
        )

    async def add_tool_result(self, result: ToolResult) -> None:
        """Add a tool result, verbatim until compaction digests it."""
        await self._add(
            kind=ItemKind.TOOL_RESULT,
            priority=Priority.RECENT_TOOL_RESULTS,
            content=result.content,
            message=Message(
                role=MessageRole.TOOL,
                content=result.content,
                tool_call_id=result.call_id,
                name=result.tool,
            ),
            tool=result.tool,
            target=result.summary.split(":")[0] if ":" in result.summary else "",
            lines=result.content.count("\n") + 1 if result.content else 0,
        )

    async def add_synthetic_result(self, call_id: CallId, text: str) -> None:
        """Add a result the harness produced itself, in place of a repeated call."""
        await self._add(
            kind=ItemKind.TOOL_RESULT,
            priority=Priority.RECENT_TOOL_RESULTS,
            content=text,
            message=Message(role=MessageRole.TOOL, content=text, tool_call_id=call_id),
        )

    async def prepare(self) -> WindowSnapshot:
        """Return the window to send, compacting it first when it is too full."""
        messages = tuple(entry.message for entry in self._entries)
        tokens = await self._tokenizer.count_messages(messages)
        if tokens < self._policy.trigger_tokens:
            return WindowSnapshot(messages=messages, tokens=tokens, fill=self._fill(tokens))

        for entry in self._entries:
            if entry.item.kind is ItemKind.TOOL_RESULT and entry.item.compactable:
                await self._measure(digest_line(entry.item))

        plan = plan_compaction(
            [entry.item for entry in self._entries], self._policy, self._lookup
        )
        summary, archived = await self._apply(plan)
        messages = tuple(entry.message for entry in self._entries)
        tokens = await self._tokenizer.count_messages(messages)
        _log.info(
            "context.compacted",
            before=plan.before_tokens,
            after=plan.after_tokens,
            measured=tokens,
            summarised=len(plan.summarized_ids),
            overflow=plan.overflow,
            archived=archived,
        )
        return WindowSnapshot(
            messages=messages,
            tokens=tokens,
            fill=self._fill(tokens),
            plan=plan,
            summary=summary,
            archived=archived,
        )

    def _fill(self, tokens: int) -> float:
        """Return window fill as a fraction of the context window, clamped to one."""
        return min(1.0, tokens / self._policy.context_window)

    async def _apply(self, plan: CompactionPlan) -> tuple[ContextSummary | None, int]:
        """Rewrite the window to match `plan`, summarising and archiving what it folds.

        Returns:
            The summary, when one was produced, and how many chunks of dropped
            content were archived.
        """
        kept = {item.id: item for item in plan.items}
        folded = [entry for entry in self._entries if entry.item.id not in kept]
        rebuilt: list[Entry] = []
        for entry in self._entries:
            item = kept.get(entry.item.id)
            if item is None:
                continue
            if item.content != entry.item.content:
                rebuilt.append(
                    Entry(
                        item=item,
                        message=entry.message.model_copy(update={"content": item.content}),
                    ),
                )
            else:
                rebuilt.append(entry)
        self._entries = rebuilt
        archived = await self._keep(folded)

        if not plan.needs_summary:
            return None, archived
        transcript = "\n\n".join(
            entry.message.content for entry in folded if entry.item.kind in TURN_KINDS
        )
        summary = await self._summarize(transcript, len(plan.summarized_ids))
        await self._insert_summary(summary.text, plan)
        return (summary if summary.text else None), archived

    async def _keep(self, folded: Sequence[Entry]) -> int:
        """Index what left the window, and never let that failure end a step.

        The window is already compacted by the time this runs, so a store that
        is down costs the model its recall and nothing else. Reporting it as a
        step failure would trade a recoverable loss for an unrecoverable one.
        """
        if self._archive is None or not folded:
            return 0
        chunks = context_chunks(
            [entry.message.content for entry in folded],
            run_id=self._archive.run_id,
            step=self._step,
        )
        if not chunks:
            return 0
        try:
            return await self._archive.store.upsert(self._archive.collection, chunks)
        except VectorStoreError as error:
            _log.warning("context.not_archived", error=str(error), chunks=len(chunks))
            return 0

    async def _summarize(self, transcript: str, folded: int) -> ContextSummary:
        """Summarise `transcript`, or state plainly that it was not summarised.

        Without a summarizer the turns are gone from the window but not from the
        run: the event log keeps them. Saying so is better than a silent gap.
        """
        if self._summarizer is None or not transcript.strip():
            text = (
                f"{folded} earlier turn(s) were compacted out of this window. "
                "They remain in the run's event log; ask for anything you need again."
            )
            return ContextSummary(text=text)
        try:
            return await self._summarizer.summarize(transcript)
        except Exception as error:  # noqa: BLE001 - a failed summary must not end a step
            _log.warning("context.summary_failed", error=str(error))
            text = (
                f"{folded} earlier turn(s) were compacted out of this window and could "
                "not be summarised. They remain in the run's event log."
            )
            return ContextSummary(text=text)

    async def _insert_summary(self, text: str, plan: CompactionPlan) -> None:
        """Put the summary where the turns it replaces used to be."""
        tokens = await self._measure(text)
        position = min(
            (index for index, entry in enumerate(self._entries) if entry.item.compactable),
            default=len(self._entries),
        )
        item = ContextItem(
            id=f"summary-{plan.before_tokens}-{plan.after_tokens}",
            priority=Priority.CURRENT_TURNS,
            kind=ItemKind.SUMMARY,
            content=text,
            tokens=tokens,
            digest=digest_text(text),
            origin_seq=self._next_seq,
        )
        self._entries.insert(
            position,
            Entry(item=item, message=Message(role=MessageRole.USER, content=text)),
        )

    def summary_digest(self, summary: ContextSummary | None) -> Digest | None:
        """Return a summary's digest, for the `context.compacted` event."""
        return summary.digest() if summary is not None else None
