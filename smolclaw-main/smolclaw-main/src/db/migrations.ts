import type { Database } from "bun:sqlite";
import { getLogger } from "../logger.ts";

interface Migration {
  version: number;
  description: string;
  up: string;
}

const migrations: Migration[] = [
  {
    version: 1,
    description: "Initial schema",
    up: `
      CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        history TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        metadata TEXT DEFAULT '{}'
      );

      CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        source TEXT NOT NULL,
        source_id TEXT,
        tags TEXT DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        hash TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS processes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        command TEXT NOT NULL,
        cwd TEXT,
        status TEXT NOT NULL DEFAULT 'stopped',
        pid INTEGER,
        restart_policy TEXT DEFAULT 'none',
        max_restarts INTEGER DEFAULT 3,
        restart_count INTEGER DEFAULT 0,
        last_exit_code INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS cron_jobs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        cron_expr TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_payload TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        last_result TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS loops (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        target_process TEXT,
        review_cron TEXT NOT NULL,
        review_prompt TEXT NOT NULL,
        apply_mode TEXT NOT NULL DEFAULT 'suggest',
        status TEXT NOT NULL DEFAULT 'active',
        cycle_count INTEGER DEFAULT 0,
        last_cycle_at TEXT,
        last_cycle_result TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
        session_id TEXT,
        action TEXT NOT NULL,
        detail TEXT NOT NULL,
        tokens_in INTEGER,
        tokens_out INTEGER
      );
    `,
  },
  {
    version: 2,
    description: "FTS5 index for memories",
    up: `
      CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        tags,
        content=memories,
        content_rowid=rowid
      );

      CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
      END;

      CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
      END;

      CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
        INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
      END;
    `,
  },
  {
    version: 3,
    description: "Vector index for memories (sqlite-vec)",
    up: `
      CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
        id TEXT PRIMARY KEY,
        embedding FLOAT[1536]
      );
    `,
  },
];

export function runMigrations(db: Database): void {
  const log = getLogger();

  // Ensure schema_version table exists
  db.exec(`
    CREATE TABLE IF NOT EXISTS schema_version (
      version INTEGER PRIMARY KEY,
      applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
  `);

  const currentVersion =
    db.query<{ version: number }, []>("SELECT MAX(version) as version FROM schema_version").get()
      ?.version ?? 0;

  log.info({ currentVersion }, "Current schema version");

  for (const migration of migrations) {
    if (migration.version <= currentVersion) continue;

    log.info({ version: migration.version, description: migration.description }, "Applying migration");

    try {
      db.exec(migration.up);
      db.prepare("INSERT INTO schema_version (version) VALUES (?)").run(migration.version);
      log.info({ version: migration.version }, "Migration applied");
    } catch (err) {
      // sqlite-vec might not be loaded — skip vector migration gracefully
      if (migration.version === 3 && String(err).includes("vec0")) {
        log.warn("Skipping vector migration — sqlite-vec not available");
        db.prepare("INSERT INTO schema_version (version) VALUES (?)").run(migration.version);
        continue;
      }
      throw err;
    }
  }
}
