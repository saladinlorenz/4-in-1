import { describe, test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { addMemory, getMemory, deleteMemory, listMemories } from "../../src/memory/store.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("memory store", () => {
  let db: Database;

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);
  });

  afterEach(() => {
    db.close();
  });

  test("addMemory creates a memory entry", async () => {
    const id = await addMemory("Test memory content", "manual", undefined, ["test"]);
    expect(id).toBeTruthy();

    const row = db.query("SELECT * FROM memories WHERE id = ?").get(id) as any;
    expect(row).toBeTruthy();
    expect(row.content).toBe("Test memory content");
    expect(row.source).toBe("manual");
    expect(JSON.parse(row.tags)).toEqual(["test"]);
  });

  test("addMemory deduplicates by hash", async () => {
    const id1 = await addMemory("Same content", "manual");
    const id2 = await addMemory("Same content", "manual");
    expect(id1).toBe(id2);

    const count = db.query<{ count: number }, []>("SELECT COUNT(*) as count FROM memories").get();
    expect(count!.count).toBe(1);
  });

  test("getMemory returns memory by ID", async () => {
    const id = await addMemory("Get test", "conversation");
    const mem = getMemory(id);
    expect(mem).toBeTruthy();
    expect(mem!.content).toBe("Get test");
    expect(mem!.source).toBe("conversation");
  });

  test("getMemory returns null for missing ID", () => {
    const mem = getMemory("nonexistent-id");
    expect(mem).toBeNull();
  });

  test("deleteMemory removes entry", async () => {
    const id = await addMemory("Delete test", "manual");
    const deleted = deleteMemory(id);
    expect(deleted).toBe(true);
    expect(getMemory(id)).toBeNull();
  });

  test("deleteMemory returns false for missing ID", () => {
    expect(deleteMemory("missing")).toBe(false);
  });

  test("listMemories returns entries", async () => {
    await addMemory("Memory A", "manual");
    await addMemory("Memory B", "conversation");
    await addMemory("Memory C", "loop");

    const list = listMemories(10);
    expect(list.length).toBe(3);
  });

  test("listMemories respects limit", async () => {
    for (let i = 0; i < 5; i++) {
      await addMemory(`Memory ${i} unique_${i}`, "manual");
    }

    const list = listMemories(2);
    expect(list.length).toBe(2);
  });
});
