import { describe, test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { addMemory } from "../../src/memory/store.ts";
import { searchMemory } from "../../src/memory/search.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("memory search", () => {
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

  test("FTS search finds matching memories", async () => {
    await addMemory("Bitcoin price is 50000 dollars", "manual", undefined, ["crypto"]);
    await addMemory("Ethereum gas fees are high", "manual", undefined, ["crypto"]);
    await addMemory("The weather is sunny today", "manual", undefined, ["weather"]);

    const results = await searchMemory("bitcoin price", 5);
    expect(results.length).toBeGreaterThanOrEqual(1);
    expect(results[0]!.content).toContain("Bitcoin");
  });

  test("returns empty array for no matches", async () => {
    const results = await searchMemory("completely unrelated query xyz", 5);
    expect(results).toEqual([]);
  });

  test("respects limit parameter", async () => {
    for (let i = 0; i < 10; i++) {
      await addMemory(`Test memory about topic number ${i}`, "manual", undefined, ["test"]);
    }

    const results = await searchMemory("test memory topic", 3);
    expect(results.length).toBeLessThanOrEqual(3);
  });

  test("results include score", async () => {
    await addMemory("Specific search term foobar", "manual");
    const results = await searchMemory("foobar", 5);

    if (results.length > 0) {
      expect(typeof results[0]!.score).toBe("number");
      expect(results[0]!.score).toBeGreaterThan(0);
    }
  });
});
