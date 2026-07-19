import json
import re
from typing import Any


def parse_json_array(raw: Any) -> list:
    """Extract the first valid JSON array from an LLM text response."""
    text = str(raw or "").lstrip("\ufeff")
    variants = (text, re.sub(r",\s*([}\]])", r"\1", text))
    last_error = None

    for candidate in variants:
        decoder = json.JSONDecoder(strict=False)
        for index, char in enumerate(candidate):
            if char != "[":
                continue
            try:
                value, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if isinstance(value, list):
                return value

    detail = f": {last_error}" if last_error else ""
    raise ValueError(f"LLM响应中没有有效的JSON数组{detail}")


def valid_merged_ids(raw_ids: Any, available_ids: set[int]) -> list[int]:
    if not isinstance(raw_ids, list):
        return []
    return sorted({
        memory_id for memory_id in raw_ids
        if isinstance(memory_id, int)
        and not isinstance(memory_id, bool)
        and memory_id in available_ids
    })
