import { v4 as uuidv4 } from "uuid";
import { createHash } from "node:crypto";
import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { getEmbedding } from "./embeddings.ts";

export interface Memory {
  id: string;
  content: string;
  source: string;
  source_id: string | null;
  tags: string[];
  created_at: string;
  updated_at: string;
  hash: string;
}

function hashContent(content: string): string {
  return createHash("sha256").update(content).digest("hex");
}

export async function addMemory(
  content: string,
  source: string,
  sourceId?: string,
  tags: string[] = []
): Promise<string> {
  const db = getDatabase();
  const log = getLogger();
  const id = uuidv4();
  const hash = hashContent(content);

  // Dedup check
  const existing = db
    .query<{ id: string }, [string]>("SELECT id FROM memories WHERE hash = ?")
    .get(hash);
  if (existing) {
    log.info({ existingId: existing.id }, "Duplicate memory, skipping");
    return existing.id;
  }

  const tagsJson = JSON.stringify(tags);
  db.prepare(
    "INSERT INTO memories (id, content, source, source_id, tags, hash) VALUES (?, ?, ?, ?, ?, ?)"
  ).run(id, content, source, sourceId ?? null, tagsJson, hash);

  // Generate and store embedding
  const embedding = await getEmbedding(content);
  if (embedding) {
    try {
      db.prepare("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)").run(id, embedding);
    } catch (err) {
      log.warn({ err }, "Failed to store embedding (sqlite-vec may not be loaded)");
    }
  }

  log.info({ id, source, tags }, "Memory added");
  return id;
}

export function getMemory(id: string): Memory | null {
  const db = getDatabase();
  const row = db
    .query<any, [string]>("SELECT * FROM memories WHERE id = ?")
    .get(id);
  if (!row) return null;
  return { ...row, tags: JSON.parse(row.tags) };
}

export function deleteMemory(id: string): boolean {
  const db = getDatabase();
  const result = db.prepare("DELETE FROM memories WHERE id = ?").run(id);
  if (result.changes > 0) {
    try {
      db.prepare("DELETE FROM memories_vec WHERE id = ?").run(id);
    } catch {
      // sqlite-vec might not be loaded
    }
    return true;
  }
  return false;
}

export function updateMemory(id: string, content: string, tags?: string[]): boolean {
  const db = getDatabase();
  const hash = hashContent(content);

  if (tags !== undefined) {
    db.prepare(
      "UPDATE memories SET content = ?, tags = ?, hash = ?, updated_at = datetime('now') WHERE id = ?"
    ).run(content, JSON.stringify(tags), hash, id);
  } else {
    db.prepare(
      "UPDATE memories SET content = ?, hash = ?, updated_at = datetime('now') WHERE id = ?"
    ).run(content, hash, id);
  }

  // Update embedding asynchronously
  getEmbedding(content).then((embedding) => {
    if (embedding) {
      try {
        db.prepare("DELETE FROM memories_vec WHERE id = ?").run(id);
        db.prepare("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)").run(id, embedding);
      } catch {
        // ignore
      }
    }
  });

  return true;
}

export function listMemories(limit = 50, offset = 0): Memory[] {
  const db = getDatabase();
  const rows = db
    .query<any, [number, number]>(
      "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    )
    .all(limit, offset);
  return rows.map((r: any) => ({ ...r, tags: JSON.parse(r.tags) }));
}
