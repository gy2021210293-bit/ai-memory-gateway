"""Pure message classification and persistence planning for the gateway."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any


Message = dict[str, Any]
DYNAMIC_ENVIRONMENT_MAX_AGE = timedelta(minutes=10)


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


def _dynamic_environment_content(content: Any) -> str | None:
    """Accept strings, or text-only lists that retain the dynamic-context envelope."""
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
    text = normalize_content_text(content)
    stripped = text.strip()
    if not (
        stripped.startswith("<dynamic_context")
        and stripped.endswith("</dynamic_context>")
    ):
        return None
    return text


def _dynamic_environment_is_fresh(message: Message) -> bool:
    metadata = message.get("metadata")
    generated_at = metadata.get("generated_at") if isinstance(metadata, dict) else None
    if not generated_at:
        return False
    if not isinstance(generated_at, str):
        return False
    try:
        generated = datetime.fromisoformat(generated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - generated.astimezone(timezone.utc)
    return -timedelta(minutes=1) <= age <= DYNAMIC_ENVIRONMENT_MAX_AGE


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
    stale_dynamic_count: int


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

    @property
    def user_leading(self) -> bool:
        """Block starts with a durable user turn (vs. a pure tool delta)."""
        return bool(self.messages) and self.messages[0].get("role") == "user"


def leading_user_messages(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """The leading user messages of a persistence block (the trigger turn)."""
    out = []
    for message in messages:
        if message.get("role") != "user":
            break
        out.append(message)
    return tuple(out)


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


def has_closed_tool_tail(messages: list[Message]) -> bool:
    """Return whether the message tail is one complete assistant/tool exchange."""
    if not messages or messages[-1].get("role") != "tool":
        return False

    tail = len(messages) - 1
    while tail >= 0 and messages[tail].get("role") == "tool":
        tail -= 1
    if tail < 0:
        return False

    assistant = messages[tail]
    if assistant.get("role") != "assistant" or not assistant.get("tool_calls"):
        return False

    expected = [
        call.get("id")
        for call in assistant["tool_calls"]
        if isinstance(call, dict) and call.get("id")
    ]
    actual = [
        message.get("tool_call_id")
        for message in messages[tail + 1:]
    ]
    return (
        bool(expected)
        and len(expected) == len(set(expected))
        and len(actual) == len(expected)
        and len(actual) == len(set(actual))
        and set(actual) == set(expected)
    )


@dataclass
class RepairResult:
    stripped_assistants: int = 0
    dropped_assistants: int = 0
    dropped_orphan_tools: int = 0
    left_for_validator: int = 0
    changed: bool = False


def _message_has_text(message: Message) -> bool:
    return bool(message_text(message).strip())


def repair_stale_tool_chains(
    messages: list[Message],
    active_tail_len: int = 0,
) -> tuple[list[Message], RepairResult]:
    """
    中和历史中"零结果"的陈旧工具链，活跃块继续由校验器严格把关。

    messages: 组装后的完整消息列表（DB历史 + 客户端增量）。
    active_tail_len: 客户端当前活跃块的条数（列表尾部）。活跃块内任何未闭合、
        错配、重复链一律不碰，继续由 validate_tool_sequence 返回 400。

    只处理两种确定"已放弃"的形状（区起点在活跃块边界之前）：
    - assistant(tool_calls) 后没有任何匹配 tool 结果 → 剥 tool_calls
      （内容为空则删整条），跟随的错误 id tool 一并删
    - 无主 tool 结果（前面没有打开的 assistant 区）→ 删
    部分闭合（有匹配但未覆盖全部 id）、闭合区内的重复 id、活跃块内的任何
    残缺 → 一律不碰，保留现有 400 行为。

    纯函数：不读写 DB、不修改输入消息对象、同输入两次结果一致（幂等）。
    """
    out: list[Message] = []
    result = RepairResult()
    boundary = len(messages) - active_tail_len

    pending_ids: set[str] | None = None
    pending_msg: Message | None = None
    zone_start = 0
    zone_results: list[Message] = []

    def close_zone() -> None:
        nonlocal pending_ids, pending_msg, zone_results
        if pending_ids is None or pending_msg is None:
            return
        matched = {
            tool.get("tool_call_id", "")
            for tool in zone_results
            if tool.get("tool_call_id", "") in pending_ids
        }
        if matched == pending_ids and pending_ids:
            # 完全闭合 → 原样保留（含跨界闭合：DB assistant + delta 结果）
            out.append(pending_msg)
            out.extend(zone_results)
        elif matched:
            # 部分闭合 → 不碰，留给校验器
            out.append(pending_msg)
            out.extend(zone_results)
            result.left_for_validator += 1
        elif zone_start < boundary:
            # 零结果 + 陈旧 → 中和整条链（剥 tool_calls / 删空消息 + 删无主 tool）
            result.dropped_orphan_tools += len(zone_results)
            if _message_has_text(pending_msg):
                pending_msg.pop("tool_calls", None)
                out.append(pending_msg)
                result.stripped_assistants += 1
            else:
                result.dropped_assistants += 1
            result.changed = True
        else:
            # 零结果 + 活跃尾 → 不碰，留给校验器
            out.append(pending_msg)
            out.extend(zone_results)
            result.left_for_validator += 1
        pending_ids = None
        pending_msg = None
        zone_results = []

    for index, message in enumerate(messages):
        role = message.get("role", "")

        if pending_ids is not None:
            if role == "tool":
                zone_results.append(message)
                continue
            close_zone()
            # 当前消息重新走下面的普通分支（close_zone 已清空状态）

        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls:
            pending_ids = {
                call.get("id", "")
                for call in tool_calls
                if isinstance(call, dict) and call.get("id")
            }
            pending_msg = deepcopy(message)
            zone_start = index
            zone_results = []
            continue

        if role == "tool":
            # 无主 tool：前面没有打开的 assistant 区
            if index < boundary:
                result.dropped_orphan_tools += 1
                result.changed = True
                continue
            out.append(message)
            continue

        out.append(message)

    close_zone()

    return out, result


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

    if cursor < 0:
        # 自包含工具链（无前导 user）：客户端以增量回传 [assistant(tool_calls), tool]，
        # 前导 user 在“完整回合才入库”的设计下尚未持久化，仍视为有效当前块。
        return [
            {key: deepcopy(value) for key, value in message.items() if key != "metadata"}
            for message in non_system
        ], True
    if non_system[cursor].get("role") != "user":
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
    dynamic_candidates = []
    invalid_dynamic = 0
    stale_dynamic = 0
    non_system_count = 0

    for message in raw:
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("dynamic_environment") is True:
            dynamic_text = _dynamic_environment_content(message.get("content"))
            if message.get("role") == "user" and dynamic_text is not None:
                if _dynamic_environment_is_fresh(message):
                    dynamic_candidates.append((non_system_count, dynamic_text))
                else:
                    stale_dynamic += 1
                continue
            else:
                invalid_dynamic += 1
        clean = {key: value for key, value in message.items() if key != "metadata"}
        ordinary.append(clean)
        if clean.get("role") != "system":
            non_system_count += 1
        if clean.get("role") == "system":
            text = _system_text(clean)
            if text:
                systems.append(text)

    current, is_tool_chain = extract_current_block(ordinary)
    current_start = non_system_count - len(current)
    dynamic = next(
        (
            text
            for position, text in reversed(dynamic_candidates)
            if position == current_start
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
        latest_user_text=_latest_user_text(current),
        invalid_dynamic_count=invalid_dynamic,
        stale_dynamic_count=stale_dynamic,
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

    # 客户端尾部是无主 tool 结果、但其重发历史里没有对应的 assistant(tool_calls)：
    # 部分插件式客户端（或格式桥）会丢掉工具调用消息。若 DB 尾部 assistant 的调用
    # id 与客户端 tool 结果完全匹配，则视为同一链的延续（DB 提供调用方、客户端
    # 提供结果），跨请求闭合，不再以 no_valid_current_block 拒绝。
    if client and client[-1].get("role") == "tool":
        tail = len(client) - 1
        while tail >= 0 and client[tail].get("role") == "tool":
            tail -= 1
        tools = client[tail + 1:]
        tool_ids = {
            tool.get("tool_call_id", "")
            for tool in tools
            if tool.get("tool_call_id")
        }
        prev = client[tail] if tail >= 0 else None
        prev_matches = bool(
            prev
            and prev.get("role") == "assistant"
            and prev.get("tool_calls")
            and {
                call.get("id", "")
                for call in prev["tool_calls"]
                if isinstance(call, dict) and call.get("id")
            }
            == tool_ids
        )
        db_last = database[-1] if database else None
        db_matches = bool(
            tool_ids
            and db_last
            and db_last.get("role") == "assistant"
            and db_last.get("tool_calls")
            and {
                call.get("id", "")
                for call in db_last["tool_calls"]
                if isinstance(call, dict) and call.get("id")
            }
            == tool_ids
        )
        if db_matches and not prev_matches:
            logical_block, is_tool_chain = extract_current_block(database + tools)
            if logical_block and is_tool_chain:
                return ReconciledBlock(
                    provider_messages=tuple(tools),
                    persistence_messages=tuple(tools),
                    latest_user_text=_latest_user_text(logical_block),
                    is_tool_chain=True,
                    aligned_count=0,
                    alignment_end=0,
                    reason="db_assistant_supplied",
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
            reason="new_current_block",
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
    leading_user_messages: tuple[Message, ...] = (),
) -> PersistencePlan:
    messages = [deepcopy(message) for message in current_block]
    if leading_user_messages:
        messages = [deepcopy(message) for message in leading_user_messages] + messages
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
