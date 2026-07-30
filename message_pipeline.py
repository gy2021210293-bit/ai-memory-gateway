"""Pure message classification and persistence planning for the gateway."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


Message = dict[str, Any]


@dataclass(frozen=True)
class ClassifiedRequest:
    raw_messages: tuple[Message, ...]
    ordinary_messages: tuple[Message, ...]
    client_system_prompts: tuple[str, ...]
    dynamic_environment: str
    current_block: tuple[Message, ...]
    is_tool_chain: bool
    latest_user_text: str
    invalid_dynamic_count: int


@dataclass(frozen=True)
class ProviderPlan:
    messages: tuple[Message, ...]
    system_prompt: str
    current_block: tuple[Message, ...]


@dataclass(frozen=True)
class PersistencePlan:
    session_id: str
    messages: tuple[Message, ...]
    completed_round: bool
    skip: bool


def message_text(message: Message) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _system_text(message: Message) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
    return ""


def extract_current_block(messages: list[Message]) -> tuple[list[Message], bool]:
    non_system = [message for message in messages if message.get("role") != "system"]
    if not non_system:
        return [], False

    cursor = len(non_system) - 1
    if non_system[cursor].get("role") == "user":
        while cursor >= 0 and non_system[cursor].get("role") == "user":
            cursor -= 1
        return [
            {key: deepcopy(value) for key, value in message.items() if key != "metadata"}
            for message in non_system[cursor + 1:]
        ], False

    if non_system[cursor].get("role") != "tool":
        return [], False

    while cursor >= 0 and non_system[cursor].get("role") == "tool":
        tool_end = cursor + 1
        while cursor >= 0 and non_system[cursor].get("role") == "tool":
            cursor -= 1
        tools = non_system[cursor + 1:tool_end]
        if cursor < 0:
            return [], False
        assistant = non_system[cursor]
        if assistant.get("role") != "assistant" or not assistant.get("tool_calls"):
            return [], False
        expected = {
            call.get("id") for call in assistant["tool_calls"] if call.get("id")
        }
        actual = [tool.get("tool_call_id") for tool in tools]
        if not expected or any(not value for value in actual) or set(actual) != expected:
            return [], False
        cursor -= 1

    if cursor < 0 or non_system[cursor].get("role") != "user":
        return [], False
    while cursor > 0 and non_system[cursor - 1].get("role") == "user":
        cursor -= 1
    return [
        {key: deepcopy(value) for key, value in message.items() if key != "metadata"}
        for message in non_system[cursor:]
    ], True


def classify_request(messages: list[Message]) -> ClassifiedRequest:
    raw = deepcopy(messages)
    ordinary = []
    systems = []
    dynamic = ""
    invalid_dynamic = 0

    for message in raw:
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("dynamic_environment") is True:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                dynamic = message["content"]
            else:
                invalid_dynamic += 1
            continue
        clean = {key: value for key, value in message.items() if key != "metadata"}
        ordinary.append(clean)
        if clean.get("role") == "system":
            text = _system_text(clean)
            if text:
                systems.append(text)

    current, is_tool_chain = extract_current_block(ordinary)
    latest_user = next(
        (
            message_text(message)
            for message in reversed(ordinary)
            if message.get("role") == "user"
        ),
        "",
    )
    return ClassifiedRequest(
        raw_messages=tuple(raw),
        ordinary_messages=tuple(ordinary),
        client_system_prompts=tuple(systems),
        dynamic_environment=dynamic,
        current_block=tuple(current),
        is_tool_chain=is_tool_chain,
        latest_user_text=latest_user,
        invalid_dynamic_count=invalid_dynamic,
    )


def combine_system_prompt(gateway_prompt: str, client_prompts: tuple[str, ...]) -> str:
    return "\n\n".join(
        part for part in (gateway_prompt, *client_prompts) if part
    )


def make_persistence_plan(
    session_id: str,
    current_block: tuple[Message, ...],
    assistant_content: str,
    assistant_tool_calls: list | None,
    assistant_reasoning: str | None,
    skip: bool,
) -> PersistencePlan:
    messages = [deepcopy(message) for message in current_block]
    assistant: Message = {"role": "assistant", "content": assistant_content or ""}
    if assistant_tool_calls:
        assistant["tool_calls"] = deepcopy(assistant_tool_calls)
    if assistant_reasoning:
        assistant["reasoning_content"] = assistant_reasoning
    if assistant_content or assistant_tool_calls:
        messages.append(assistant)
    return PersistencePlan(
        session_id=session_id,
        messages=tuple(messages),
        completed_round=bool(messages and not assistant_tool_calls and assistant_content),
        skip=skip,
    )
