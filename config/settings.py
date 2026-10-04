from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LLMEndpoint(BaseModel):
    base_url: str
    api_key: str = ""
    model: str = "auto"
    timeout: float = 60.0
    name: str = ""
    enabled: bool = True
    priority: int = 100
    attempts: int = 0  # 0 = router default
    cooldown_seconds: float = 0.0  # 0 = router default
    max_tokens: int | None = None
    temperature: float | None = None

    @property
    def label(self) -> str:
        from urllib.parse import urlparse

        host = urlparse(self.base_url).netloc or self.base_url
        return f"{host}/{self.model}"

    @property
    def display_name(self) -> str:
        return self.name or self.label


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "agentos"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    storage_dir: Path = PROJECT_ROOT / "storage"

    llm_endpoints: list[LLMEndpoint] = Field(default_factory=list)
    llm_max_attempts: int = 2
    llm_backoff_base: float = 1.5
    llm_cooldown_seconds: float = 30.0
    llm_tool_choice: str = "required"

    agent_max_steps: int = 12
    agent_max_output_chars: int = 4000

    workflow_retry_limit: int = 1
    workflow_retry_backoff_base: float = 1.5

    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    confirmation_ttl_hours: float = 24.0
    stuck_task_hours: float = 2.0

    telegram_bot_token: str = ""
    telegram_admin_chat_id: int = 0
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)
    telegram_send_timeout: float = 30.0

    health_host: str = "127.0.0.1"
    health_port: int = 8080

    web_search_max_results: int = 5
    web_search_timeout: float = 15.0
    web_fetch_timeout: float = 20.0
    web_fetch_max_bytes: int = 1_500_000
    web_fetch_max_chars: int = 20_000

    files_max_bytes: int = 200_000
    files_max_entries: int = 200

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().strip("[]")
            if not text:
                return []
            try:
                return [int(part.strip()) for part in text.split(",") if part.strip()]
            except ValueError:
                return value
        return value

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "agentos.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.storage_dir / "files"

    @property
    def logs_dir(self) -> Path:
        return self.storage_dir / "logs"

    @property
    def allowed_user_ids(self) -> set[int]:
        ids = set(self.telegram_allowed_user_ids)
        if self.telegram_admin_chat_id:
            ids.add(self.telegram_admin_chat_id)
        return ids

    def ensure_dirs(self) -> None:
        for path in (self.storage_dir, self.files_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: str | Path | None = PROJECT_ROOT / ".env") -> Settings:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    if raw and not raw.strip().startswith("["):
        try:
            parsed = [int(part.strip()) for part in raw.split(",") if part.strip()]
        except ValueError:
            parsed = None
        if parsed is not None:
            os.environ["TELEGRAM_ALLOWED_USER_IDS"] = json.dumps(parsed)
    return Settings(_env_file=env_file)
