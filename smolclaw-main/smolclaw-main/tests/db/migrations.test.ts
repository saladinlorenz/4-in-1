import { describe, test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("migrations", () => {
  let db: Database;

  beforeEach(() => {
    db = new Database(":memory:");
  });

  afterEach(() => {
    db.close();
  });

  test("creates schema_version table", () => {
    runMigrations(db);
    const tables = db
      .query<{ name: string }, []>("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
      .all();
    expect(tables.length).toBe(1);
  });

  test("creates all core tables", () => {
    runMigrations(db);
    const tables = db
      .query<{ name: string }, []>(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
      )
      .all()
      .map((t) => t.name);

    expect(tables).toContain("sessions");
    expect(tables).toContain("memories");
    expect(tables).toContain("processes");
    expect(tables).toContain("cron_jobs");
    expect(tables).toContain("loops");
    expect(tables).toContain("audit_log");
    expect(tables).toContain("schema_version");
  });

  test("records migration versions", () => {
    runMigrations(db);
    const versions = db
      .query<{ version: number }, []>("SELECT version FROM schema_version ORDER BY version")
      .all();

    expect(versions.length).toBeGreaterThanOrEqual(2);
    expect(versions[0]!.version).toBe(1);
    expect(versions[1]!.version).toBe(2);
  });

  test("is idempotent", () => {
    runMigrations(db);
    runMigrations(db);
    const versions = db
      .query<{ version: number }, []>("SELECT version FROM schema_version")
      .all();
    expect(versions.length).toBeGreaterThanOrEqual(2);
  });

  test("creates FTS5 virtual table", () => {
    runMigrations(db);
    const fts = db
      .query<{ name: string }, []>(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
      )
      .all();
    expect(fts.length).toBe(1);
  });

  test("sessions table has correct columns", () => {
    runMigrations(db);
    const info = db.query<{ name: string }, []>("PRAGMA table_info(sessions)").all();
    const cols = info.map((c) => c.name);
    expect(cols).toContain("id");
    expect(cols).toContain("history");
    expect(cols).toContain("created_at");
    expect(cols).toContain("updated_at");
    expect(cols).toContain("metadata");
  });
});
