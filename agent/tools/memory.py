from __future__ import annotations

from smolagents import tool

from config.logging import redact

from .base import ToolDeps


def make_tools(deps: ToolDeps) -> list[object]:
    @tool
    def remember(text: str) -> str:
        """Save a durable fact or note into the agent's long term memory.

        Args:
            text: The fact or note to remember.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return "Nothing was saved: empty note."
        try:
            identifier = deps.storage.remember(cleaned, source="agent")
        except Exception as exc:
            return f"Memory save failed: {redact(str(exc))}"
        return f"Saved to memory as #{identifier}."

    @tool
    def search_memory(query: str, limit: int = 5) -> str:
        """Search the agent's long term memory for previously saved facts.

        Args:
            query: Keywords to look for in memory.
            limit: Maximum number of memories to return, between 1 and 20.
        """
        if not query or not query.strip():
            return "No memories matched."
        count = min(20, max(1, int(limit)))
        try:
            rows = deps.storage.search_memory(query.strip(), limit=count)
        except Exception as exc:
            return f"Memory search failed: {redact(str(exc))}"
        if not rows:
            return "No memories matched."
        return "\n".join(f"- [{row['created_at']}] {row['text']}" for row in rows)

    return [remember, search_memory]
