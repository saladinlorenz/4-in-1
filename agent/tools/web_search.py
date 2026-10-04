from __future__ import annotations

from smolagents import tool

from config.logging import redact

from .base import ToolDeps


def default_search(query: str, max_results: int, timeout: float) -> list[dict]:
    from ddgs import DDGS

    with DDGS(timeout=max(1, int(timeout))) as client:
        return client.text(query, max_results=max_results)


def make_tool(deps: ToolDeps, timeout: float = 15.0) -> object:
    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the web with DuckDuckGo and return titles, URLs and snippets.

        Args:
            query: The search query to send to DuckDuckGo.
            max_results: How many results to return, between 1 and 10.
        """
        if not query or not query.strip():
            return "Search failed: empty query."
        count = min(10, max(1, int(max_results)))
        try:
            results = deps.search_fn(query.strip(), count)
        except Exception as exc:
            return f"Search failed: {redact(str(exc))}"
        if not results:
            return "No results found."
        lines: list[str] = []
        for index, item in enumerate(results[:count], start=1):
            title = str(item.get("title") or "").strip()
            url = str(item.get("href") or item.get("url") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()[:300]
            lines.append(f"{index}. {title}\n   {url}\n   {snippet}")
        return "\n".join(lines)

    return web_search
