from __future__ import annotations

from smolagents import tool

from config.logging import redact

from .base import ToolDeps


def make_tools(deps: ToolDeps) -> list[object]:
    @tool
    def model_generate(prompt: str) -> str:
        """Call the language model directly for a quick standalone completion
        (no mission, no tools). Use for summarizing, rewriting or translating.

        Args:
            prompt: The instruction to complete.
        """
        text = (prompt or "").strip()
        if not text:
            return "Generation failed: prompt is empty."
        if len(text) > 20_000:
            return "Generation failed: prompt too long (20000 chars max)."
        if deps.generate_fn is None:
            return "Generation unavailable: no model in this context."
        try:
            return deps.generate_fn(text)
        except Exception as exc:
            return f"Generation failed: {redact(str(exc))[:500]}"

    return [model_generate]
