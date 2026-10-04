from __future__ import annotations

from smolagents import tool

from config.logging import redact

from .base import ToolDeps


def make_tools(deps: ToolDeps) -> list[object]:
    @tool
    def social_create_draft(platform: str, content: str, scheduled_for: str = "") -> str:
        """Create (or reuse) a social media draft stored in SQLite.

        Args:
            platform: Target platform, for example telegram, devto or bluesky.
            content: Full text of the draft.
            scheduled_for: Optional UTC ISO datetime expressing the editorial intent.
        """
        name = (platform or "").strip().lower()
        text = (content or "").strip()
        if not name:
            return "Draft not created: platform is required."
        if not text:
            return "Draft not created: content is empty."
        try:
            existing = deps.storage.find_draft(name, text)
            if existing is not None:
                return (
                    f"Draft #{existing['id']} already exists for {name} "
                    f"(status {existing['status']})."
                )
            draft_id = deps.storage.create_draft(
                name, text, scheduled_for=(scheduled_for or None)
            )
        except Exception as exc:
            return f"Draft creation failed: {redact(str(exc))}"
        return f"Draft #{draft_id} created for {name} (status DRAFT)."

    @tool
    def social_publish(draft_id: int) -> str:
        """Request publication of a draft. Publication only happens after the
        operator approves the confirmation on Telegram.

        Args:
            draft_id: Identifier of the draft to publish.
        """
        try:
            draft = deps.storage.get_draft(int(draft_id))
            if draft is None:
                return f"Draft #{draft_id} not found."
            if draft["status"] == "PUBLISHED":
                return (
                    f"Draft #{draft_id} already published at {draft['published_at']} "
                    f"({draft['platform']})."
                )
            confirmation = deps.storage.find_confirmation(
                "publish_draft", f"draft#{int(draft_id)}"
            )
            if confirmation is None:
                if deps.request_confirmation is None:
                    return "Publishing unavailable: no confirmation channel in this context."
                confirm_id = deps.request_confirmation(
                    "publish_draft", f"draft#{int(draft_id)}"
                )
                if confirm_id is None:
                    return "Publishing refused: the current task cannot hold a confirmation."
                return (
                    f"Confirmation #{confirm_id} required to publish draft #{draft_id}. "
                    f"Nothing was published. The mission resumes after /approve {confirm_id}."
                )
            status = confirmation["status"]
            if status == "PENDING":
                return (
                    f"Waiting for confirmation #{confirmation['id']}: "
                    f"/approve {confirmation['id']} — nothing published."
                )
            if status == "REJECTED":
                return (
                    f"Publishing draft #{draft_id} was rejected "
                    f"(confirmation #{confirmation['id']})."
                )
            if status == "EXPIRED":
                return (
                    f"Confirmation #{confirmation['id']} for draft #{draft_id} expired "
                    f"before publication."
                )
            return (
                f"Draft #{draft_id} is approved; current status: {draft['status']}. "
                f"Publication is processed automatically on approval."
            )
        except Exception as exc:
            return f"Publish failed: {redact(str(exc))}"

    @tool
    def social_list_drafts(status: str = "", limit: int = 10) -> str:
        """List stored social drafts with their status and schedule.

        Args:
            status: Optional status filter: DRAFT, PUBLISHED or FAILED.
            limit: Maximum number of drafts to return, between 1 and 50.
        """
        count = min(50, max(1, int(limit)))
        try:
            rows = deps.storage.list_drafts(count, status=(status.strip().upper() or None))
        except Exception as exc:
            return f"Draft listing failed: {redact(str(exc))}"
        if not rows:
            return "No drafts found."
        lines = []
        for row in rows:
            preview = str(row["content"]).replace("\n", " ")[:60]
            scheduled = f" scheduled={row['scheduled_for']}" if row.get("scheduled_for") else ""
            lines.append(
                f"#{row['id']} [{row['status']}] {row['platform']} {row['created_at']}"
                f"{scheduled} {preview}"
            )
        return "\n".join(lines)

    return [social_create_draft, social_publish, social_list_drafts]
