from __future__ import annotations

from pathlib import Path

from smolagents import tool

from .base import ToolDeps


def resolve_in_sandbox(root: Path, relative: str) -> Path:
    if not relative or not relative.strip():
        raise ValueError("empty path")
    candidate_text = relative.strip().replace("\\", "/")
    if candidate_text.startswith("/") or (len(candidate_text) > 1 and candidate_text[1] == ":"):
        raise ValueError("absolute paths are not allowed")
    root_resolved = root.resolve()
    candidate = (root_resolved / candidate_text).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError("path escapes the sandbox")
    return candidate


def display_path(root: Path, target: Path) -> str:
    return str(target.relative_to(root.resolve())).replace("\\", "/")


def make_tools(deps: ToolDeps) -> list[object]:
    root = deps.files_dir
    max_bytes = deps.files_max_bytes
    max_entries = deps.files_max_entries

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file inside the agent sandbox.

        Args:
            path: Relative file path inside the sandbox, for example notes/todo.txt.
            content: The text content to write.
        """
        try:
            target = resolve_in_sandbox(root, path)
        except ValueError as exc:
            return f"Write failed: {exc}"
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            return f"Write failed: content exceeds {max_bytes} bytes."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        except OSError as exc:
            return f"Write failed: {exc}"
        return f"Wrote {len(encoded)} bytes to {display_path(root, target)}"

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the agent sandbox.

        Args:
            path: Relative file path inside the sandbox, for example notes/todo.txt.
        """
        try:
            target = resolve_in_sandbox(root, path)
        except ValueError as exc:
            return f"Read failed: {exc}"
        if not target.is_file():
            return f"Read failed: {path} does not exist."
        data = target.read_bytes()
        if len(data) > max_bytes:
            return f"Read failed: file exceeds {max_bytes} bytes."
        return data.decode("utf-8", errors="replace")

    @tool
    def list_files(path: str = ".") -> str:
        """List files inside the agent sandbox directory.

        Args:
            path: Relative directory path inside the sandbox, defaults to the root.
        """
        try:
            target = resolve_in_sandbox(root, path)
        except ValueError as exc:
            return f"List failed: {exc}"
        if not target.is_dir():
            return f"List failed: {path} is not a directory."
        entries: list[str] = []
        for item in sorted(target.rglob("*")):
            if item.is_file():
                entries.append(f"{display_path(root, item)} ({item.stat().st_size} bytes)")
                if len(entries) >= max_entries:
                    entries.append("...[truncated]")
                    break
        if not entries:
            return "The sandbox is empty."
        return "\n".join(entries)

    return [write_file, read_file, list_files]
