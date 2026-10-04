from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from config.secrets import SecretStore
from config.settings import LLMEndpoint

from .settings_service import SettingsService, validation_message

KEY_PREFIX = "llm.endpoint."
SECRET_PREFIX = "LLM_ENDPOINT_KEY_"
CATEGORY = "models"
ACTOR = "dashboard"


class EndpointInput(BaseModel):
    """Write-only endpoint payload: ``api_key`` is never persisted in SQLite."""

    name: str = ""
    base_url: str
    model: str = "auto"
    timeout: float = Field(60.0, gt=0, le=600)
    enabled: bool = True
    priority: int = Field(100, ge=0, le=1000)
    attempts: int = Field(0, ge=0, le=10)
    cooldown_seconds: float = Field(0.0, ge=0, le=3600)
    max_tokens: int | None = Field(None, gt=0, le=1_000_000)
    temperature: float | None = Field(None, ge=0, le=2)
    api_key: str | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        clean = (value or "").strip().rstrip("/")
        if not clean.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if len(clean) > 500 or "\n" in clean:
            raise ValueError("base_url is invalid")
        return clean

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        clean = (value or "").strip()
        if len(clean) > 64 or "\n" in clean or "\r" in clean:
            raise ValueError("name must be a single line of at most 64 chars")
        return clean

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\n" in value or "\r" in value or len(value) > 512:
            raise ValueError("api_key must be a single line of at most 512 chars")
        return value

    def stored_value(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=False)
        data.pop("api_key", None)
        return data


class LLMConfigService:
    """CRUD for LLM endpoints: config in SQLite, keys in the SecretStore.

    Every mutation reloads the live ``LLMRouter`` (hot reload, no restart).
    API keys are write-only: they go straight to the SecretStore and only a
    masked status ever comes back through the API.
    """

    def __init__(
        self,
        settings_service: SettingsService,
        secret_store: SecretStore,
        router: Any,
        *,
        actor: str = ACTOR,
    ) -> None:
        self.settings = settings_service
        self.secrets = secret_store
        self.router = router
        self.actor = actor

    # --- storage helpers -------------------------------------------------

    def _rows(self) -> list[dict[str, Any]]:
        rows = []
        for item in self.settings.list(CATEGORY):
            key = str(item.get("key", ""))
            if not key.startswith(KEY_PREFIX) or not isinstance(item.get("value"), dict):
                continue
            suffix = key[len(KEY_PREFIX):]
            if suffix.isdigit():
                rows.append({"id": int(suffix), "value": item["value"]})
        return sorted(rows, key=lambda row: (row["value"].get("priority", 100), row["id"]))

    def _row(self, endpoint_id: int) -> dict[str, Any]:
        for row in self._rows():
            if row["id"] == endpoint_id:
                return row
        raise ValueError(f"unknown endpoint id: {endpoint_id}")

    def _next_id(self) -> int:
        ids = [row["id"] for row in self._rows()]
        return max(ids, default=0) + 1

    @staticmethod
    def _key(endpoint_id: int) -> str:
        return f"{KEY_PREFIX}{endpoint_id}"

    @staticmethod
    def _secret_name(endpoint_id: int) -> str:
        return f"{SECRET_PREFIX}{endpoint_id}"

    @staticmethod
    def _validate(data: dict[str, Any]) -> EndpointInput:
        try:
            return EndpointInput(**data)
        except Exception as exc:
            raise ValueError(validation_message(exc)) from None

    def _to_endpoint(self, endpoint_id: int, value: dict[str, Any]) -> LLMEndpoint:
        api_key = self.secrets.get(self._secret_name(endpoint_id)) or ""
        return LLMEndpoint(**value, api_key=api_key)

    def _public(self, endpoint_id: int, value: dict[str, Any]) -> dict[str, Any]:
        masked = self.secrets.mask(self._secret_name(endpoint_id))
        return {
            "id": endpoint_id,
            **value,
            "api_key_configured": masked is not None,
            "api_key_masked": masked or "",
        }

    # --- lifecycle -------------------------------------------------------

    def seed(self, env_endpoints: list[LLMEndpoint]) -> int:
        """Bootstrap SQLite from ``.env`` once: config moves, keys move."""
        if self._rows():
            return 0
        count = 0
        for endpoint in env_endpoints:
            endpoint_id = self._next_id()
            if endpoint.api_key:
                self.secrets.set(self._secret_name(endpoint_id), endpoint.api_key)
            self.settings.set(
                self._key(endpoint_id),
                endpoint.model_dump(exclude={"api_key"}),
                category=CATEGORY,
                actor=self.actor,
            )
            count += 1
        if count:
            self.settings.storage.add_audit(
                self.actor, "llm.seed", f"endpoints={count}"
            )
        return count

    def list(self) -> list[dict[str, Any]]:
        return [self._public(row["id"], row["value"]) for row in self._rows()]

    def health(self) -> list[dict[str, Any]]:
        return self.router.health()

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self._validate(payload)
        endpoint_id = self._next_id()
        value = endpoint.stored_value()
        if endpoint.api_key:
            self.secrets.set(self._secret_name(endpoint_id), endpoint.api_key)
        self.settings.set(self._key(endpoint_id), value, category=CATEGORY, actor=self.actor)
        self.settings.storage.add_audit(
            self.actor, "llm.endpoint.create", f"id={endpoint_id}"
        )
        self.reload()
        return self._public(endpoint_id, value)

    def update(self, endpoint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._row(endpoint_id)["value"]
        merged = {**current, **payload}
        endpoint = self._validate(merged)
        value = endpoint.stored_value()
        secret_name = self._secret_name(endpoint_id)
        if endpoint.api_key is not None:
            if endpoint.api_key:
                self.secrets.set(secret_name, endpoint.api_key)
            else:
                self.secrets.delete(secret_name)
        self.settings.set(self._key(endpoint_id), value, category=CATEGORY, actor=self.actor)
        self.settings.storage.add_audit(
            self.actor, "llm.endpoint.update", f"id={endpoint_id}"
        )
        self.reload()
        return self._public(endpoint_id, value)

    def delete(self, endpoint_id: int) -> bool:
        self._row(endpoint_id)
        removed = self.settings.delete(self._key(endpoint_id))
        self.secrets.delete(self._secret_name(endpoint_id))
        if removed:
            self.settings.storage.add_audit(
                self.actor, "llm.endpoint.delete", f"id={endpoint_id}"
            )
            self.reload()
        return removed

    def reload(self) -> int:
        endpoints = [self._to_endpoint(row["id"], row["value"]) for row in self._rows()]
        self.router.reload(endpoints)
        return len(endpoints)

    def test(self, endpoint_id: int) -> dict[str, Any]:
        row = self._row(endpoint_id)
        endpoint = self._to_endpoint(endpoint_id, row["value"])
        result = self.router.test_endpoint(endpoint)
        self.settings.storage.add_audit(
            self.actor, "llm.endpoint.test", f"id={endpoint_id} ok={result.get('ok')}"
        )
        return {"id": endpoint_id, **result}
