import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { getEmbedding } from "./embeddings.ts";
import type { Memory } from "./store.ts";

interface SearchResult extends Memory {
  score: number;
}

/**
 * Hybrid search: combines vector similarity (sqlite-vec) + FTS5 keyword search
 * using Reciprocal Rank Fusion (RRF) to merge rankings.
 */
export async function searchMemory(query: string, limit = 10): Promise<SearchResult[]> {
  const db = getDatabase();
  const log = getLogger();

  const vectorResults = await vectorSearch(query, limit * 2);
  const ftsResults = ftsSearch(query, limit * 2);

  // Reciprocal Rank Fusion
  const k = 60; // RRF constant
  const scores = new Map<string, { score: number; memory: Memory }>();

  vectorResults.forEach((r, rank) => {
    const rrf = 1 / (k + rank + 1);
    const existing = scores.get(r.id);
    if (existing) {
      existing.score += rrf;
    } else {
      scores.set(r.id, { score: rrf, memory: r });
    }
  });

  ftsResults.forEach((r, rank) => {
    const rrf = 1 / (k + rank + 1);
    const existing = scores.get(r.id);
    if (existing) {
      existing.score += rrf;
    } else {
      scores.set(r.id, { score: rrf, memory: r });
    }
  });

  const merged = Array.from(scores.values())
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((r) => ({ ...r.memory, score: r.score }));

  log.debug(
    { query, vectorCount: vectorResults.length, ftsCount: ftsResults.length, mergedCount: merged.length },
    "Memory search completed"
  );

  return merged;
}

async function vectorSearch(query: string, limit: number): Promise<Memory[]> {
  const db = getDatabase();
  const log = getLogger();

  const queryEmbedding = await getEmbedding(query);
  if (!queryEmbedding) return [];

  try {
    const rows = db
      .query<{ id: string; distance: number }, [Float32Array, number]>(
        `SELECT id, distance FROM memories_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?`
      )
      .all(queryEmbedding, limit);

    const memories: Memory[] = [];
    for (const row of rows) {
      const mem = db
        .query<any, [string]>("SELECT * FROM memories WHERE id = ?")
        .get(row.id);
      if (mem) {
        memories.push({ ...mem, tags: JSON.parse(mem.tags) });
      }
    }
    return memories;
  } catch (err) {
    log.debug({ err }, "Vector search unavailable, falling back to FTS only");
    return [];
  }
}

function ftsSearch(query: string, limit: number): Memory[] {
  const db = getDatabase();

  try {
    // FTS5 search with BM25 ranking
    const rows = db
      .query<{ rowid: number }, [string, number]>(
        `SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?`
      )
      .all(query, limit);

    const memories: Memory[] = [];
    for (const row of rows) {
      const mem = db
        .query<any, [number]>("SELECT * FROM memories WHERE rowid = ?")
        .get(row.rowid);
      if (mem) {
        memories.push({ ...mem, tags: JSON.parse(mem.tags) });
      }
    }
    return memories;
  } catch (err) {
    getLogger().debug({ err }, "FTS search failed, query may have invalid syntax");
    return [];
  }
}
