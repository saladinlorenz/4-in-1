from __future__ import annotations
import sqlite3

DDL = """
CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  intelligence_rank INTEGER NOT NULL,
  speed_rank INTEGER NOT NULL,
  size_label TEXT NOT NULL DEFAULT '',
  rpm_limit INTEGER,
  rpd_limit INTEGER,
  tpm_limit INTEGER,
  tpd_limit INTEGER,
  monthly_token_budget TEXT NOT NULL DEFAULT '',
  context_window INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  supports_vision INTEGER NOT NULL DEFAULT 0,
  supports_tools INTEGER NOT NULL DEFAULT 0,
  paid_input_per_m REAL,
  paid_output_per_m REAL,
  source TEXT NOT NULL DEFAULT 'catalog',
  endpoint_scope TEXT NOT NULL DEFAULT '',
  available INTEGER NOT NULL DEFAULT 1,
  key_id INTEGER REFERENCES api_keys(id),
  UNIQUE(platform, model_id, endpoint_scope)
);
CREATE INDEX IF NOT EXISTS idx_models_endpoint_scope ON models(endpoint_scope) WHERE endpoint_scope != '';
CREATE INDEX IF NOT EXISTS idx_models_platform ON models(platform);

CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  encrypted_key TEXT NOT NULL,
  iv TEXT NOT NULL,
  auth_tag TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unknown',
  enabled INTEGER NOT NULL DEFAULT 1,
  base_url TEXT,
  model_scope_json TEXT,
  proxy_encrypted TEXT,
  proxy_iv TEXT,
  proxy_auth_tag TEXT,
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_keys_platform ON api_keys(platform);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  key_id INTEGER REFERENCES api_keys(id),
  status TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  ttfb_ms INTEGER,
  error TEXT,
  requested_model TEXT,
  endpoint_scope TEXT NOT NULL DEFAULT '',
  client_ip TEXT,
  client_user_agent TEXT,
  client_agent TEXT,
  pinned_model_id TEXT,
  served_model TEXT,
  request_type TEXT NOT NULL DEFAULT 'chat',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_requests_platform ON requests(platform);
CREATE INDEX IF NOT EXISTS idx_requests_key_id ON requests(key_id);

CREATE TABLE IF NOT EXISTS request_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER REFERENCES requests(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  key_ordinal INTEGER,
  platform TEXT,
  model_id TEXT,
  key_id INTEGER,
  outcome TEXT NOT NULL,
  start_offset_ms INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  key_label TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rate_limit_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  key_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('request','tokens')),
  tokens INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_usage_lookup ON rate_limit_usage(platform, model_id, key_id, kind, created_at_ms);

CREATE TABLE IF NOT EXISTS rate_limit_cooldowns (
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  key_id INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'heuristic',
  set_at_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (platform, model_id, key_id)
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_cooldowns_expires ON rate_limit_cooldowns(expires_at_ms);

CREATE TABLE IF NOT EXISTS fallback_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_db_id INTEGER NOT NULL REFERENCES models(id),
  priority INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE(model_db_id)
);

CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  emoji TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '#6366f1',
  type TEXT NOT NULL DEFAULT 'custom',
  is_favorite INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  auto_sort TEXT,
  layout_config TEXT,
  auto_include_new_models INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  model_db_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  priority INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE(profile_id, model_db_id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS provider_quota_state (
  platform TEXT NOT NULL,
  key_id INTEGER NOT NULL,
  quota_pool_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  limit_value INTEGER,
  remaining_value INTEGER,
  reset_at TEXT,
  reset_strategy TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL DEFAULT 'probe',
  confidence REAL NOT NULL DEFAULT 0,
  notes TEXT,
  observed_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (platform, key_id, quota_pool_key, metric)
);

CREATE TABLE IF NOT EXISTS provider_quota_observations (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  key_id INTEGER NOT NULL,
  provider_account_id TEXT,
  model_id TEXT,
  quota_pool_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  status_code INTEGER,
  limit_value INTEGER,
  remaining_value INTEGER,
  reset_at TEXT,
  retry_after_ms INTEGER,
  reset_strategy TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL DEFAULT 'probe',
  confidence REAL NOT NULL DEFAULT 0,
  notes TEXT,
  raw_json TEXT,
  endpoint TEXT,
  observed_at TEXT NOT NULL DEFAULT (datetime('now')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_overrides (
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  overrides_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (platform, model_id)
);

CREATE TABLE IF NOT EXISTS catalog_model_tombstones (
  kind TEXT NOT NULL,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (kind, platform, model_id)
);

CREATE TABLE IF NOT EXISTS quirks (
  slug TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quirk_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quirk_id TEXT NOT NULL REFERENCES quirks(slug) ON DELETE CASCADE,
  platform TEXT,
  model_glob TEXT
);

CREATE TABLE IF NOT EXISTS embedding_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family TEXT NOT NULL,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  dimensions INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  key_id INTEGER REFERENCES api_keys(id),
  endpoint_scope TEXT NOT NULL DEFAULT '',
  max_input_tokens INTEGER,
  quota_label TEXT NOT NULL DEFAULT '',
  UNIQUE(platform, model_id, endpoint_scope)
);

CREATE TABLE IF NOT EXISTS media_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  modality TEXT NOT NULL,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  key_id INTEGER REFERENCES api_keys(id),
  endpoint_scope TEXT NOT NULL DEFAULT '',
  quota_label TEXT NOT NULL DEFAULT '',
  UNIQUE(platform, model_id, endpoint_scope, modality)
);

CREATE TABLE IF NOT EXISTS playground_conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  model TEXT,
  system_prompt TEXT,
  messages_json TEXT NOT NULL DEFAULT '[]',
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS server_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  level TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS api_url_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS client_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS custom_model_tombstones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  model_id TEXT NOT NULL,
  endpoint_scope TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(platform, model_id, endpoint_scope)
);

CREATE TABLE IF NOT EXISTS migrations (
  name TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def init_schema(conn: sqlite3.Connection):
    conn.executescript(DDL)
    conn.commit()
    # ensure migrations table has baseline
    try:
        conn.execute("INSERT OR IGNORE INTO migrations(name) VALUES('baseline')")
        conn.commit()
    except Exception:
        pass
    ensure_default_settings(conn)

def ensure_default_settings(conn: sqlite3.Connection):
    defaults = {
        "unified_api_key": "",
        "active_profile_id": "",
        "routing_strategy": "balanced",
        "routing_weights": '{"reliability":0.5,"speed":0.25,"intelligence":0.25}',
        "expose_fallback_detail_header": "0",
        "fallback_time_budget_ms": "45000",
        "max_consecutive_upstream_fails": "0",
        "validate_tool_arguments": "0",
        "ollama_emulation": "off",
        "expose_cc_discovery_aliases": "0",
        "request_max_tokens_budget": "0",
        "update_check_enabled": "0",
        "catalog_last_sync_ms": "",
        "catalog_applied_json": "[]",
        "total_requests": "0",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    conn.commit()

def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None

def set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
