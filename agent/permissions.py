from __future__ import annotations

from collections.abc import Iterable

ALLOWED_TOOL_NAMES = frozenset(
    {
        "web_search",
        "web_fetch",
        "write_file",
        "read_file",
        "list_files",
        "remember",
        "search_memory",
        "send_notification",
        "get_status",
        "social_create_draft",
        "social_publish",
        "social_list_drafts",
        "model_generate",
        "workflow_create",
        "workflow_run",
        "workflow_status",
        "final_answer",
    }
)


def is_authorized_user(user_id: int | None, allowed: Iterable[int]) -> bool:
    allowed_set = set(allowed)
    if not allowed_set:
        return False
    return user_id is not None and user_id in allowed_set


def filter_tools(tools: Iterable[object], allowed: Iterable[str] = ALLOWED_TOOL_NAMES) -> list[object]:
    allowed_set = set(allowed)
    filtered = []
    for item in tools:
        name = getattr(item, "name", None)
        if name is None or name in allowed_set:
            filtered.append(item)
    return filtered
