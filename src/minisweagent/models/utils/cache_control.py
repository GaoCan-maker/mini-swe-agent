"""Cache control utilities are mostly for Anthropic models.
They are used to explicitly set cache control points.
"""

import copy
import warnings
from typing import Literal


def _get_content_text(entry: dict) -> str | None:
    if entry["content"] is None:
        return None
    if isinstance(entry["content"], str):
        return entry["content"]
    assert len(entry["content"]) == 1, "Expected single message in content"
    return entry["content"][0]["text"]


def _clear_cache_control(entry: dict) -> None:
    if isinstance(entry["content"], list):
        assert len(entry["content"]) == 1, "Expected single message in content"
        entry["content"][0].pop("cache_control", None)
    # Note: entry["content"] can be None for assistant messages with only tool_use
    entry.pop("cache_control", None)


def _set_cache_control(entry: dict) -> None:
    # Handle None content (e.g., assistant messages with only tool_use)
    if entry["content"] is None:
        entry["cache_control"] = {"type": "ephemeral"}
        return

    if not isinstance(entry["content"], list):
        entry["content"] = [  # type: ignore
            {
                "type": "text",
                "text": _get_content_text(entry),
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        entry["content"][0]["cache_control"] = {"type": "ephemeral"}
    if entry["role"] == "tool":
        # Workaround for weird bug
        entry["content"][0].pop("cache_control", None)
        entry["cache_control"] = {"type": "ephemeral"}


def cache_breakpoints(messages: list[dict]) -> list[int]:
    """Indices of messages that an agent flagged with `extra.cache_breakpoint`."""
    return [i for i, m in enumerate(messages) if m.get("extra", {}).get("cache_breakpoint")]


def set_cache_control(
    messages: list[dict],
    *,
    mode: Literal["default_end"] | None = "default_end",
    last_n_messages_offset: int = 0,
    breakpoints: list[int] | None = None,
) -> list[dict]:
    """This messages processor adds manual cache control marks to the last message and to `breakpoints`."""
    if mode is None:
        return messages
    if mode != "default_end":
        raise ValueError(f"Invalid mode: {mode}")
    if last_n_messages_offset:
        warnings.warn("last_n_messages_offset is deprecated and will be removed in the future. It has no effect.")

    messages = copy.deepcopy(messages)
    marked = {len(messages) - 1, *(breakpoints or [])}
    for i_entry, entry in enumerate(messages):
        _clear_cache_control(entry)
        if i_entry in marked:
            _set_cache_control(entry)
    return messages
