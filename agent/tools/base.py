from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class NotifierLike(Protocol):
    def send(self, text: str) -> None: ...


@dataclass
class ToolDeps:
    storage: Any
    notifier: NotifierLike
    status_fn: Callable[[], dict]
    search_fn: Callable[[str, int], list[dict]]
    fetch_fn: Callable[[str], str]
    files_dir: Path
    files_max_bytes: int = 200_000
    files_max_entries: int = 200
    web_fetch_max_chars: int = 20_000
    request_confirmation: Callable[[str, str], int | None] | None = None
