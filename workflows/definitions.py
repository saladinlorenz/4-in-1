"""Built-in workflow definitions registered at startup by main.py."""

from __future__ import annotations

DEFAULT_WORKFLOWS: dict[str, list[dict]] = {
    "daily_ai_news": [
        {
            "name": "search",
            "kind": "agent",
            "prompt": (
                "Search the web for the most recent AI agents news from the last 24 hours. "
                "Return a compact bullet list with titles, sources and URLs."
            ),
        },
        {
            "name": "digest",
            "kind": "agent",
            "prompt": (
                "Write a daily digest of the most important AI news found, maximum 8 bullet "
                "points. Context: {context}"
            ),
        },
        {
            "name": "archive",
            "kind": "agent",
            "prompt": (
                "Save the digest to long term memory with the remember tool, "
                "starting each note with 'Daily AI digest:'."
            ),
        },
    ],
    "daily_report": [
        {
            "name": "status",
            "kind": "agent",
            "prompt": "Call get_status and report the current system status as a short summary.",
        },
        {
            "name": "archive",
            "kind": "agent",
            "prompt": "Save this status snapshot to memory with the remember tool.",
        },
    ],
}

DEFAULT_SCHEDULED_JOBS: list[dict] = [
    {"name": "daily_ai_news_0800", "workflow_name": "daily_ai_news", "cron": {"hour": 8, "minute": 0}},
    {"name": "daily_report_1800", "workflow_name": "daily_report", "cron": {"hour": 18, "minute": 0}},
]
