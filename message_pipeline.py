"""Pure message classification and persistence planning for the gateway."""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any


Message = dict[str, Any]


def normalize_content_text(content: Any) -> str:
    """Convert OpenAI-compatible message content to database-safe text."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    text_parts.append(text)
        return "\n".join(text_parts) if text_parts else "[图片]"
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(content)


def _pure_text_content(content: Any) -> str | None:
    """Return text only when content is a non-empty string or text-only list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    for block in content:
        if isinstance(block, str):
            continue
        if not (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text", ""), str)
        ):
            return None
    return normalize_content_text(content)


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


@dataclass(frozen=True)
class ReconciledBlock:
    provider_messages: tuple[Message, ...]
    persistence_messages: tuple[Message, ...]
    latest_user_text: str
    is_tool_chain: bool
    aligned_count: int
    alignment_end: int
    reason: str


@dataclass(frozen=True)
class ToolSequenceValidation:
    valid: bool
    index: int = -1
    role: str = ""
    reason: str = ""
    pending_ids: tuple[str, ...] = ()
    tool_call_id: str = ""


def _empty_reconciled(reason: str, aligned_count: int = 0, alignment_end: int = -1):
    return ReconciledBlock(
        provider_messages=(),
        persistence_messages=(),
        latest_user_text="",
        is_tool_chain=False,
        aligned_count=aligned_count,
        alignment_end=alignment_end,
        reason=reason,
    )


def validate_tool_sequence(messages: list[Message]) -> ToolSequenceValidation:
    """Validate OpenAI assistant(tool_calls) -> tool* message ordering."""
    pending_ids: set[str] = set()
    completed_ids: set[str] = set()

    for index, message in enumerate(messages):
        role = message.get("role", "")

        if pending_ids:
            if role != "tool":
                return ToolSequenceValidation(
                    valid=False,
                    index=index,
                    role=role,
                    reason="unclosed_tool_calls",
                    pending_ids=tuple(sorted(pending_ids)),
                )

            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                return ToolSequenceValidation(
                    valid=False,
                    index=index,
                    role=role,
                    reason="missing_tool_call_id",
                    pending_ids=tuple(sorted(pending_ids)),
                )
            if tool_call_id not in pending_ids:
                return ToolSequenceValidation(
                    valid=False,
                    index=index,
                    role=role,
                    reason=(
                        "duplicate_tool_result"
                        if tool_call_id in completed_ids
                        else "unexpected_tool_call_id"
                    ),
                    pending_ids=tuple(sorted(pending_ids)),
                    tool_call_id=tool_call_id,
                )

            pending_ids.remove(tool_call_id)
            completed_ids.add(tool_call_id)
            continue

        if role == "tool":
            return ToolSequenceValidation(
                valid=False,
                index=index,
                role=role,
                reason="orphan_tool_result",
                tool_call_id=message.get("tool_call_id", ""),
            )

        tool_calls = message.get("tool_calls")
        if role != "assistant" or not tool_calls:
            continue

        call_ids = []
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(call_id, str) or not call_id:
                return ToolSequenceValidation(
                    valid=False,
                    index=index,
                    role=role,
                    reason="missing_tool_call_id",
                )
            if call_id in call_ids:
                return ToolSequenceValidation(
                    valid=False,
                    index=index,
                    role=role,
                    reason="duplicate_tool_call_id",
                    tool_call_id=call_id,
                )
            call_ids.append(call_id)

        pending_ids = set(call_ids)
        completed_ids = set()

    if pending_ids:
        return ToolSequenceValidation(
            valid=False,
            index=len(messages),
            role="end",
            reason="unclosed_tool_calls",
            pending_ids=tuple(sorted(pending_ids)),
        )

    return ToolSequenceValidation(valid=True)


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


def _latest_user_text(messages: list[Message]) -> str:
    return next(
        (
            message_text(message)
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )


def message_signature(message: Message) -> tuple:
    tool_call_ids = tuple(sorted(
        call.get("id")
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    ))
    return (
        message.get("role", ""),
        message_text(message),
        tool_call_ids,
        message.get("tool_call_id", ""),
    )


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
            dynamic_text = _pure_text_content(message.get("content"))
            if message.get("role") == "user" and dynamic_text is not None:
                dynamic = dynamic_text
                continue
            else:
                invalid_dynamic += 1
        clean = {key: value for key, value in message.items() if key != "metadata"}
        ordinary.append(clean)
        if clean.get("role") == "system":
            text = _system_text(clean)
            if text:
                systems.append(text)

    current, is_tool_chain = extract_current_block(ordinary)
    return ClassifiedRequest(
        raw_messages=tuple(raw),
        ordinary_messages=tuple(ordinary),
        client_system_prompts=tuple(systems),
        dynamic_environment=dynamic,
        current_block=tuple(current),
        is_tool_chain=is_tool_chain,
        latest_user_text=_latest_user_text(current),
        invalid_dynamic_count=invalid_dynamic,
    )


def reconcile_partition_block(
    database_messages: list[Message],
    client_messages: list[Message],
) -> ReconciledBlock:
    database = [
        message for message in database_messages
        if message.get("role") != "system"
    ]
    client = [
        {key: deepcopy(value) for key, value in message.items() if key != "metadata"}
        for message in client_messages
        if message.get("role") != "system"
    ]

    database_signatures = [message_signature(message) for message in database]
    client_signatures = [message_signature(message) for message in client]
    candidates = []
    maximum = min(len(database_signatures), len(client_signatures))
    for size in range(maximum, 0, -1):
        database_suffix = database_signatures[-size:]
        candidates = [
            end
            for end in range(size, len(client_signatures) + 1)
            if client_signatures[end - size:end] == database_suffix
        ]
        if candidates:
            if len(candidates) != 1:
                return _empty_reconciled(
                    "ambiguous_history_alignment",
                    aligned_count=size,
                )
            alignment_end = candidates[0]
            delta = client[alignment_end:]
            if not delta:
                return _empty_reconciled(
                    "no_new_client_messages",
                    aligned_count=size,
                    alignment_end=alignment_end,
                )
            logical_block, is_tool_chain = extract_current_block(database + delta)
            if not logical_block:
                return _empty_reconciled(
                    "invalid_aligned_message_sequence",
                    aligned_count=size,
                    alignment_end=alignment_end,
                )
            return ReconciledBlock(
                provider_messages=tuple(delta),
                persistence_messages=tuple(delta),
                latest_user_text=_latest_user_text(logical_block),
                is_tool_chain=is_tool_chain,
                aligned_count=size,
                alignment_end=alignment_end,
                reason="aligned",
            )

    fallback, is_tool_chain = extract_current_block(client)
    if fallback:
        return ReconciledBlock(
            provider_messages=tuple(fallback),
            persistence_messages=tuple(fallback),
            latest_user_text=_latest_user_text(fallback),
            is_tool_chain=is_tool_chain,
            aligned_count=0,
            alignment_end=0,
            reason="strict_tail_fallback",
        )

    return _empty_reconciled("no_valid_current_block")


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
