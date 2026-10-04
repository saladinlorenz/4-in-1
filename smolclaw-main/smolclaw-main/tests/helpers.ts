import pino from "pino";
import { Database } from "bun:sqlite";
import { setLogger } from "../src/logger.ts";
import { setDatabase } from "../src/db/connection.ts";
import { runMigrations } from "../src/db/migrations.ts";

const silentLog = pino({ level: "silent" });

/**
 * Initialize a silent logger for tests.
 */
export function setupTestLogger() {
  setLogger(silentLog);
}

/**
 * Create and initialize an in-memory test database with migrations applied.
 */
export function setupTestDb(): Database {
  setupTestLogger();
  const db = new Database(":memory:");
  db.exec("PRAGMA journal_mode = WAL");
  setDatabase(db);
  runMigrations(db);
  return db;
}
