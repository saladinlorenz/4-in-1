import { Database } from "bun:sqlite";
import { existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { getLogger } from "../logger.ts";

let db: Database | undefined;

export function initDatabase(dbPath: string): Database {
  const dir = dirname(dbPath);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }

  db = new Database(dbPath, { create: true });

  // Enable WAL mode for concurrent reads
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("PRAGMA synchronous = NORMAL");
  db.exec("PRAGMA foreign_keys = ON");
  db.exec("PRAGMA busy_timeout = 5000");

  // Try loading sqlite-vec extension
  try {
    const sqliteVec = require("sqlite-vec");
    sqliteVec.load(db);
    getLogger().info("sqlite-vec extension loaded");
  } catch (err) {
    getLogger().warn({ err }, "sqlite-vec extension not available, vector search disabled");
  }

  return db;
}

/**
 * Set database directly (used for testing with in-memory DBs).
 */
export function setDatabase(database: Database): void {
  db = database;
}

export function getDatabase(): Database {
  if (!db) {
    throw new Error("Database not initialized. Call initDatabase() first.");
  }
  return db;
}

export function closeDatabase(): void {
  if (db) {
    db.close();
    db = undefined;
  }
}
