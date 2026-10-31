"""The one scripted model.

Every graph test drives the model from a fixed list of replies so the graph, not the model,
is what is under test. It used to be copied into four test files; a module team would have
made it five.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from app.integration.model_client import ModelReply, StreamChunk


def chunks(steps: list) -> Iterator[StreamChunk]:
    """The step vocabulary every streaming double speaks, in one place.

    A step is a string (text), a `("reasoning", text)` pair, a `("call", name, arguments)`
    triple, a `("fail", exception)` pair raised mid-stream, or a `("truncated",)` marker that
    ends the stream without a finish signal.
    """

    call_index = 0
    for step in steps:
        if isinstance(step, str):
            yield StreamChunk(text_delta=step)
        elif step[0] == "reasoning":
            yield StreamChunk(reasoning_delta=step[1])
        elif step[0] == "fail":
            # The interesting kind of failure: the steps before it already reached the
            # caller, so it cannot be answered by starting the stream over.
            raise step[1]
        elif step[0] == "truncated":
            # The provider closed the stream without a finish signal.
            yield StreamChunk(truncated=True)
            return
        else:
            # Parallel calls get distinct indices, the way a provider sends them: the
            # consumer keeps each call's argument stream apart by index, and two calls
            # on one index would splice into one broken JSON document.
            yield StreamChunk(tool_name=step[1], tool_call_delta=step[2], tool_index=call_index)
            call_index += 1
    yield StreamChunk(finish=True)


class ScriptedModel:
    """Replies from a fixed script, so a test can drive the graph deterministically.

    One script, two shapes: `stream` answers a decision turn with the step the script names
    (text, a tool call, a failure), and `complete` answers a turn that asks for plain text --
    the context summarizer. Both pop from the same list, so a graph that calls one and then
    the other keeps its replies in call order.
    """

    def __init__(self, replies: list) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        # The whole call, snapshot at its start -- the same record `StreamingModel` keeps,
        # for the tests that assert what the model was actually handed.
        self.messages: list[tuple[dict, ...]] = []
        self.tools: list[tuple[dict, ...]] = []
        # How many decision turns went through the native tool-calling path, as opposed to
        # the plain-text "complete" call. A test that asserts the model was asked for a
        # decision with tools counts this, not the total number of calls.
        self.streams = 0

    def complete(self, messages: tuple[dict, ...]) -> ModelReply:
        self.messages.append(tuple(dict(message) for message in messages))
        self.prompts.append(messages[0]["content"])
        if not self._replies:
            raise AssertionError("the script ran out of replies")
        return ModelReply(text=self._replies.pop(0))

    def stream(self, messages, *, tools=()):
        self.messages.append(tuple(dict(message) for message in messages))
        self.prompts.append(messages[0]["content"])
        self.tools.append(tuple(tools))
        self.streams += 1
        if not self._replies:
            raise AssertionError("the script ran out of replies")
        reply = self._replies.pop(0)
        # A reply is one step, or a list of steps when a turn carries several calls.
        yield from chunks(reply if isinstance(reply, list) else [reply])

    def close(self) -> None:
        return None


class RoutingModel:
    """One double for two callers: the supervisor decides, the assembler summarizes.

    A summary prompt is recognized by its marker and answered with a JSON object; every
    other prompt is a decision, answered from the script. `prompts` keeps every prompt in
    order so a test can assert what the model was shown; `decision_prompts` keeps only the
    decisions, which is what assertions about the conversation want. `summary_prompts` and
    `summary_messages` keep the summary calls: the user turn alone, and the whole call.
    """

    SUMMARY_MARKER = "对话历史的压缩器"

    def __init__(self, decisions: list[str], summaries: tuple[str, ...] = ("历史摘要",)) -> None:
        self._decisions = list(decisions)
        self._summaries = list(summaries)
        self.prompts: list[str] = []
        self.summary_prompts: list[str] = []  # the user content of each summary call
        # The whole call, not just the user turn: production sends a system message too, and a
        # budget assertion that only sees the user turn cannot see the system message at all.
        self.summary_messages: list[tuple[dict, ...]] = []

    @property
    def decision_prompts(self) -> list[str]:
        return [prompt for prompt in self.prompts if self.SUMMARY_MARKER not in prompt]

    def complete(self, messages: tuple[dict, ...]) -> ModelReply:
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if self.SUMMARY_MARKER in prompt:
            self.summary_prompts.append(messages[-1]["content"])
            self.summary_messages.append(tuple(messages))
            text = self._summaries.pop(0) if self._summaries else "历史摘要"
            return ModelReply(text=json.dumps({"summary": text}, ensure_ascii=False))
        if not self._decisions:
            raise AssertionError("the script ran out of decisions")
        return ModelReply(text=self._decisions.pop(0))

    def stream(self, messages, *, tools=()):
        # A decision turn under the one decision path: the same script, read as a step.
        self.prompts.append(messages[0]["content"])
        if not self._decisions:
            raise AssertionError("the script ran out of decisions")
        yield from chunks([self._decisions.pop(0)])

    def close(self) -> None:
        return None


class StreamingModel:
    """A model whose turns all arrive as chunks, one scripted list per call.

    `decisions` is one list of steps per streamed turn, so a two-round conversation is two
    entries; a step is a string (text), a `("reasoning", text)` pair, a
    `("call", name, arguments)` triple, a `("fail", exception)` pair raised mid-stream, or a
    `("truncated",)` marker that ends the stream without a finish signal. `errors` is popped
    once per stream call and raised instead of replaying a decision. `replies` answers the
    `complete` calls (the context summarizer).
    """

    def __init__(
        self, decisions: list, replies: list | None = None, errors: list | None = None
    ) -> None:
        self._decisions = [list(decision) for decision in decisions]
        self._replies = list(replies or [])
        self._errors = list(errors or [])
        self.prompts: list[str] = []
        self.tools: list[tuple[dict, ...]] = []
        # The whole call, snapshot at its start: a loop feeds a tool's answer back and calls
        # again, so "what did the model see the second time" is an assertion about messages,
        # not just about the first line. Copied per message, so thread edits after the call
        # cannot rewrite what was recorded.
        self.messages: list[tuple[dict[str, Any], ...]] = []
        self.completed: list[str] = []
        self.closed = False
        # Counts the decision turns that arrived as chunks, the same record ScriptedModel
        # keeps: a graph that still asks for one JSON blob leaves this at zero.
        self.streams = 0

    def stream(self, messages, *, tools=()):
        self.messages.append(tuple(dict(message) for message in messages))
        self.prompts.append(messages[0]["content"])
        self.tools.append(tuple(tools))
        self.streams += 1
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        yield from chunks(self._decisions.pop(0))

    def complete(self, messages):
        self.completed.append(messages[0]["content"])
        if not self._replies:
            raise AssertionError("the script ran out of replies")
        return ModelReply(text=self._replies.pop(0))

    def close(self) -> None:
        self.closed = True
