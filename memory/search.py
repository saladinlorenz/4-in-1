from __future__ import annotations

import re
from typing import Any

from .sqlite_store import Storage


def _score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(term) for term in terms)


def search_facts(storage: Storage, query: str, limit: int = 5) -> str:
    rows = storage.search_memory(query, limit=max(limit * 3, limit))
    if not rows:
        return "No matching memories."
    terms = [term.lower() for term in re.findall(r"\w+", query) if len(term) > 2]
    ranked: list[tuple[int, dict[str, Any]]] = [(_score(row["text"], terms), row) for row in rows]
    ranked.sort(key=lambda item: (-item[0], -int(item[1]["id"])))
    lines = []
    for _, row in ranked[:limit]:
        lines.append(f"- [{row['created_at']}] {row['text']}")
    return "\n".join(lines)
