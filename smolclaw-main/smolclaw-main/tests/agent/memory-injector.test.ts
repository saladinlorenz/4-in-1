import { describe, test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import { writeFileSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { injectMemory } from "../../src/agent/memory-injector.ts";
import { addMemory } from "../../src/memory/store.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

const TEST_MEM_DIR = "/tmp/smolclaw-test-memory";

describe("memory injector", () => {
  let db: Database;

  const config: any = {
    workspace: { dir: "/tmp", memoryDir: TEST_MEM_DIR },
    openai: { apiKey: "test", embeddingModel: "test" },
    daemon: { logLevel: "info", dataDir: "/tmp" },
  };

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);

    if (existsSync(TEST_MEM_DIR)) rmSync(TEST_MEM_DIR, { recursive: true });
    mkdirSync(TEST_MEM_DIR, { recursive: true });
  });

  afterEach(() => {
    db.close();
    if (existsSync(TEST_MEM_DIR)) rmSync(TEST_MEM_DIR, { recursive: true });
  });

  test("returns undefined when no directives and no memories", async () => {
    const result = await injectMemory("hello", config);
    expect(result).toBeUndefined();
  });

  test("includes DIRECTIVES.md when present", async () => {
    writeFileSync(`${TEST_MEM_DIR}/DIRECTIVES.md`, "Always be helpful.\nPrefer concise answers.");

    const result = await injectMemory("hello", config);
    expect(result).toBeTruthy();
    expect(result).toContain("DIRECTIVES");
    expect(result).toContain("Always be helpful");
  });

  test("includes relevant memories from search", async () => {
    await addMemory("The user prefers dark mode", "conversation");
    await addMemory("Bitcoin bot runs on port 8080", "manual");

    const result = await injectMemory("bitcoin bot", config);
    if (result) {
      expect(result).toContain("Relevant Memories");
    }
  });

  test("combines directives and memories", async () => {
    writeFileSync(`${TEST_MEM_DIR}/DIRECTIVES.md`, "Core directive: be precise.");
    await addMemory("Server runs on port 3000", "manual");

    const result = await injectMemory("server port", config);
    expect(result).toBeTruthy();
    expect(result).toContain("Core directive");
  });
});
