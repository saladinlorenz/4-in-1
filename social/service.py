from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.logging import redact

logger = logging.getLogger(__name__)

PUBLISH_KIND = "publish_draft"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_draft_payload(payload: str) -> int | None:
    if not payload.startswith("draft#"):
        return None
    try:
        return int(payload.removeprefix("draft#"))
    except ValueError:
        return None


class SocialService:
    """Draft lifecycle: create -> confirm -> publish -> verify.

    Publication happens only inside ``on_confirmation`` when the operator
    approved (``/approve <id>``). The adapter must return a real reference;
    failures mark the draft FAILED and are notified. Nothing is ever faked.
    """

    def __init__(
        self,
        storage: object,
        runner: object,
        adapters: dict[str, object] | None = None,
    ) -> None:
        self.storage = storage
        self.runner = runner
        self.adapters: dict[str, object] = dict(adapters or {})

    def add_adapter(self, adapter: object) -> None:
        platform = str(getattr(adapter, "platform", "")).strip().lower()
        if platform:
            self.adapters[platform] = adapter

    # --- confirmation listener (the only publication path) ---------------

    def on_confirmation(self, confirmation_id: int, decision: str) -> None:
        confirmation = self.storage.get_confirmation(confirmation_id)
        if confirmation is None or confirmation["kind"] != PUBLISH_KIND:
            return
        draft_id = _parse_draft_payload(str(confirmation["payload"]))
        if draft_id is None:
            logger.warning("publish confirmation %s has an invalid payload", confirmation_id)
            return
        draft = self.storage.get_draft(draft_id)
        if draft is None:
            logger.warning("publish confirmation %s points to a missing draft", confirmation_id)
            return
        if decision != "APPROVED":
            self.runner.notify(
                f"Social: draft #{draft_id} was not published ({decision.lower()})."
            )
            return
        if draft["status"] == "PUBLISHED":
            return
        self._publish(draft)

    def _publish(self, draft: dict) -> None:
        draft_id = int(draft["id"])
        platform = str(draft["platform"]).strip().lower()
        adapter = self.adapters.get(platform)
        if adapter is None:
            self.storage.update_draft(
                draft_id, status="FAILED", error=f"no adapter configured for {platform}"
            )
            self.runner.notify(
                f"Social: draft #{draft_id} NOT published — no adapter for {platform}."
            )
            return
        try:
            receipt = str(adapter.publish(draft["content"]))
        except Exception as exc:  # adapter failure must not kill the decision flow
            detail = redact(f"{type(exc).__name__}: {exc}")[:300]
            self.storage.update_draft(draft_id, status="FAILED", error=detail)
            self.runner.notify(f"Social: draft #{draft_id} publish FAILED: {detail}")
            logger.warning("publish draft %s failed: %s", draft_id, detail)
            return
        self.storage.update_draft(
            draft_id, status="PUBLISHED", published_at=_now(), error=None
        )
        self.runner.notify(
            f"Social: draft #{draft_id} published on {platform} — {receipt}"
        )
