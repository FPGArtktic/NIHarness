# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The window measures with the tokenizer, compacts by the policy, and keeps the protocol."""

from collections.abc import Sequence

import pytest

from niharness.application.window import MeasurementMissingError, Window
from niharness.domain.artifacts import ContextSummary
from niharness.domain.context import CompactionPolicy, ItemKind
from niharness.domain.identity import CallId
from niharness.domain.messages import Message, MessageRole, ToolCallRequest
from niharness.domain.tools import ToolName, ToolResult

POLICY = CompactionPolicy(
    context_window=1_024,
    threshold=0.5,
    keep_last_tool_results=1,
    reserve_response_tokens=128,
)


class FakeTokenizer:
    """Counts a token per four characters, and three per message for the template."""

    def __init__(self) -> None:
        self.text_calls = 0
        self.message_calls = 0

    async def count_text(self, text: str) -> int:
        self.text_calls += 1
        return max(1, len(text) // 4)

    async def count_messages(self, messages: Sequence[Message]) -> int:
        self.message_calls += 1
        return sum(max(1, len(message.content) // 4) + 3 for message in messages)


class FakeSummarizer:
    """Returns a fixed summary and records what it was asked to summarise."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def summarize(self, transcript: str) -> ContextSummary:
        self.transcripts.append(transcript)
        return ContextSummary(
            decisions=("keep the parser in the domain",),
            text=(
                "Earlier: decided to keep the parser in the domain; "
                "two patches failed to apply."
            ),
        )


def tool_result(
    call_id: str,
    content: str,
    *,
    path: str = "",
    tool: ToolName = ToolName.READ_FILE,
) -> ToolResult:
    target = path or f"src/{call_id}.py"
    return ToolResult(
        call_id=CallId(call_id),
        tool=tool,
        ok=True,
        content=content,
        summary=f"{target}: {content.count(chr(10)) + 1} lines",
    )


async def seeded(window: Window) -> Window:
    await window.add_prompt("policy and role layers")
    await window.add_task("task: add the parser")
    return window


async def test_a_window_under_the_threshold_is_sent_as_is() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    await window.add_assistant("I will read the file.")
    snapshot = await window.prepare()
    assert snapshot.plan is None
    assert not snapshot.compacted
    assert [message.role for message in snapshot.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert 0.0 < snapshot.fill < 0.5


async def test_order_is_chronological_and_tool_messages_follow_their_call() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    await window.add_assistant(
        "",
        [ToolCallRequest(call_id=CallId("c1"), name="read_file", arguments_json="{}")],
    )
    await window.add_tool_result(tool_result("c1", "file body"))
    await window.add_assistant("now I know what it says")
    snapshot = await window.prepare()
    roles = [message.role for message in snapshot.messages]
    assert roles == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert snapshot.messages[2].tool_calls[0].call_id == "c1"
    assert snapshot.messages[3].tool_call_id == "c1"
    assert snapshot.messages[3].name == "read_file"


async def test_old_tool_results_become_digest_lines() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    for index in range(4):
        await window.add_tool_result(tool_result(f"c{index}", "x" * 600))
    snapshot = await window.prepare()
    assert snapshot.plan is not None
    contents = [
        message.content for message in snapshot.messages if message.role is MessageRole.TOOL
    ]
    assert any("sha" in content and "lines" in content for content in contents)
    assert contents[-1] == "x" * 600, "the newest result stays verbatim"


async def test_the_prompt_and_the_task_survive_compaction() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    for index in range(6):
        await window.add_tool_result(tool_result(f"c{index}", f"result {index} " + "y" * 600))
    snapshot = await window.prepare()
    assert snapshot.messages[0].content == "policy and role layers"
    assert snapshot.messages[1].content == "task: add the parser"


async def test_reading_the_same_file_twice_is_kept_once() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    for index in range(4):
        await window.add_tool_result(tool_result(f"c{index}", "z" * 700, path="src/same.py"))
    snapshot = await window.prepare()
    bodies = [m.content for m in snapshot.messages if m.role is MessageRole.TOOL]
    assert bodies.count("z" * 700) == 1


async def test_turns_are_summarised_when_digesting_is_not_enough() -> None:
    summarizer = FakeSummarizer()
    window = await seeded(Window(FakeTokenizer(), POLICY, summarizer=summarizer))
    for index in range(8):
        await window.add_assistant(f"turn {index}: " + "w" * 400)
    snapshot = await window.prepare()
    assert snapshot.plan is not None
    assert snapshot.plan.needs_summary
    assert summarizer.transcripts, "the folded turns were handed to the summarizer"
    assert snapshot.summary is not None
    assert any("Earlier:" in message.content for message in snapshot.messages)


async def test_without_a_summarizer_the_gap_is_stated_not_hidden() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    for index in range(8):
        await window.add_assistant(f"turn {index}: " + "w" * 400)
    snapshot = await window.prepare()
    assert snapshot.plan is not None
    assert snapshot.plan.needs_summary
    pointer = [m for m in snapshot.messages if "event log" in m.content]
    assert pointer, "the window says where the dropped turns went"


async def test_a_failing_summarizer_does_not_lose_the_window() -> None:
    class Broken:
        async def summarize(self, transcript: str) -> ContextSummary:
            msg = "the utility model returned prose"
            raise ValueError(msg)

    window = await seeded(Window(FakeTokenizer(), POLICY, summarizer=Broken()))
    for index in range(8):
        await window.add_assistant(f"turn {index}: " + "w" * 400)
    snapshot = await window.prepare()
    assert any("could not be summarised" in message.content for message in snapshot.messages)


async def test_every_count_comes_from_the_tokenizer() -> None:
    tokenizer = FakeTokenizer()
    window = await seeded(Window(tokenizer, POLICY))
    await window.add_tool_result(tool_result("c1", "body"))
    snapshot = await window.prepare()
    assert tokenizer.text_calls >= 3
    assert tokenizer.message_calls >= 1
    assert snapshot.tokens > 0


async def test_measuring_the_same_text_twice_costs_one_call() -> None:
    tokenizer = FakeTokenizer()
    window = Window(tokenizer, POLICY)
    await window.add_assistant("the same text")
    await window.add_assistant("the same text")
    assert tokenizer.text_calls == 1


async def test_an_unmeasured_string_is_an_error_not_a_guess() -> None:
    window = Window(FakeTokenizer(), POLICY)
    with pytest.raises(MeasurementMissingError, match="never measured"):
        window._lookup("a string nobody measured")


async def test_synthetic_results_enter_the_window_as_tool_messages() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    await window.add_synthetic_result(CallId("c9"), "this call already ran")
    snapshot = await window.prepare()
    last = snapshot.messages[-1]
    assert last.role is MessageRole.TOOL
    assert last.tool_call_id == "c9"


async def test_the_window_records_what_each_item_is() -> None:
    window = await seeded(Window(FakeTokenizer(), POLICY))
    await window.add_notes("NOTES.md: the mirror has no pyserial")
    await window.add_instruction("the call was refused: not_allowed")
    kinds = [entry.item.kind for entry in window.entries]
    assert kinds == [
        ItemKind.PROMPT_LAYER,
        ItemKind.TASK,
        ItemKind.NOTES,
        ItemKind.INSTRUCTION,
    ]
