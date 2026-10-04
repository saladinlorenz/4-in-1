from __future__ import annotations

from typing import Protocol


class SocialAdapter(Protocol):
    """Publishes a draft to a real platform and returns a reference (URL, message id...).

    Implementations must perform a real API call and raise on failure.
    A publication is never simulated: no adapter, no publication.
    """

    platform: str

    def publish(self, content: str) -> str: ...
