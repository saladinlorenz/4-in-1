from __future__ import annotations

from html.parser import HTMLParser

import httpx
from smolagents import tool

from config.logging import redact

from .base import ToolDeps


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template", "svg"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    return parser.text()


def default_fetch(url: str, timeout: float, max_bytes: int) -> str:
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    with httpx.Client(follow_redirects=True, timeout=timeout) as client, client.stream(
        "GET", url
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        encoding = response.encoding or "utf-8"
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) >= max_bytes:
                break
    text = bytes(data).decode(encoding, errors="replace")
    if "html" in content_type:
        text = html_to_text(text)
    return text


def make_tool(deps: ToolDeps) -> object:
    @tool
    def web_fetch(url: str) -> str:
        """Fetch a web page over HTTP and return its readable text content.

        Args:
            url: The http or https URL to fetch.
        """
        if not url or not url.strip():
            return "Fetch failed: empty URL."
        try:
            text = deps.fetch_fn(url.strip())
        except Exception as exc:
            return f"Fetch failed: {redact(str(exc))}"
        text = text.strip()
        if not text:
            return "The page returned no readable text."
        limit = deps.web_fetch_max_chars
        if len(text) > limit:
            text = text[:limit] + "\n...[truncated]"
        return text

    return web_fetch
