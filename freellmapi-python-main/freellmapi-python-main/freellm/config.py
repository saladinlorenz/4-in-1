from __future__ import annotations
import os
from dataclasses import dataclass, field

DEFAULT_RPM = 120
DEFAULT_ADMIN_RPM = 600
DEFAULT_REQUEST_BODY_LIMIT_MB = 25
DEFAULT_PORT = 3001
DEFAULT_HOST = "::"

def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        n = int(float(raw.strip()))
        if n < 0:
            return default
        return n
    except ValueError:
        return default

def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        n = float(raw.strip())
        if n != n or n == float("inf") or n == float("-inf"):
            return default
        return n
    except ValueError:
        return default

def _parse_bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    v = raw.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None

@dataclass
class Config:
    port: int | str = DEFAULT_PORT
    host: str = DEFAULT_HOST
    db_path: str | None = None
    dashboard_origins: list[str] = field(default_factory=list)
    client_dist: str | None = None
    proxy_rate_limit_rpm: int = DEFAULT_RPM
    admin_rate_limit_rpm: int = DEFAULT_ADMIN_RPM
    request_body_limit_bytes: int = DEFAULT_REQUEST_BODY_LIMIT_MB * 1024 * 1024
    node_env: str = "development"
    serve_static: bool = True
    csp_upgrade_insecure: bool | None = None
    encryption_key: str | None = None
    fallback_time_budget_ms: int = 45000
    provider_timeout_default: int = 15000
    provider_stall_timeout_ms: int = 90000
    response_cache_enabled: bool = False
    response_cache_ttl_s: int = 3600
    request_analytics_retention_days: int = 90
    request_analytics_max_rows: int = 100000

def load_config() -> Config:
    port_raw = os.getenv("PORT", str(DEFAULT_PORT))
    try:
        port: int | str = int(port_raw)
    except ValueError:
        port = port_raw
    host = os.getenv("HOST", DEFAULT_HOST)
    db_path = (os.getenv("FREEAPI_DB_PATH") or "").strip() or None
    origins = [s.strip() for s in (os.getenv("DASHBOARD_ORIGINS") or "").split(",") if s.strip()]
    client_dist = os.getenv("CLIENT_DIST")
    proxy_rpm = _parse_int_env("PROXY_RATE_LIMIT_RPM", DEFAULT_RPM)
    admin_rpm = _parse_int_env("ADMIN_RATE_LIMIT_RPM", DEFAULT_ADMIN_RPM)
    body_mb_raw = os.getenv("REQUEST_BODY_LIMIT_MB")
    if body_mb_raw and body_mb_raw.strip():
        try:
            mb = int(float(body_mb_raw.strip()))
            body_bytes = max(1, mb) * 1024 * 1024
        except ValueError:
            body_bytes = DEFAULT_REQUEST_BODY_LIMIT_MB * 1024 * 1024
    else:
        body_bytes = DEFAULT_REQUEST_BODY_LIMIT_MB * 1024 * 1024
    node_env = os.getenv("NODE_ENV") or os.getenv("ENV") or "development"
    csp = _parse_bool_env("CSP_UPGRADE_INSECURE_REQUESTS")
    enc = os.getenv("ENCRYPTION_KEY")
    fallback_budget = _parse_int_env("FALLBACK_TIME_BUDGET_MS", 45000)
    stall = _parse_int_env("PROVIDER_STREAM_STALL_TIMEOUT_MS", 90000)
    cache_enabled = _parse_bool_env("RESPONSE_CACHE")
    if cache_enabled is None:
        cache_enabled = False
    cache_ttl = _parse_int_env("RESPONSE_CACHE_TTL_SECONDS", 3600)
    retention_days = _parse_int_env("REQUEST_ANALYTICS_RETENTION_DAYS", 90)
    max_rows = _parse_int_env("REQUEST_ANALYTICS_MAX_ROWS", 100000)
    return Config(
        port=port,
        host=host,
        db_path=db_path,
        dashboard_origins=origins,
        client_dist=client_dist,
        proxy_rate_limit_rpm=proxy_rpm,
        admin_rate_limit_rpm=admin_rpm,
        request_body_limit_bytes=body_bytes,
        node_env=node_env,
        serve_static=True,
        csp_upgrade_insecure=csp,
        encryption_key=enc,
        fallback_time_budget_ms=fallback_budget,
        provider_stall_timeout_ms=stall,
        response_cache_enabled=cache_enabled,
        response_cache_ttl_s=cache_ttl,
        request_analytics_retention_days=retention_days,
        request_analytics_max_rows=max_rows,
    )

_config: Config | None = None

def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config
