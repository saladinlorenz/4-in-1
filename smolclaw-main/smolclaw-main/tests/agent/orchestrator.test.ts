import { describe, test, expect, beforeEach, afterEach, mock } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { createOrchestrator } from "../../src/agent/orchestrator.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("orchestrator", () => {
  let db: Database;

  const config: any = {
    anthropic: { apiKey: "test-key", model: "claude-opus-4-6", maxTokens: 1000 },
    workspace: { dir: "/tmp", memoryDir: "/tmp/mem" },
    telegram: { botToken: "test", allowedUsers: [123], allowedUsernames: [] },
    openai: { apiKey: "test", embeddingModel: "test" },
    daemon: { logLevel: "info", dataDir: "/tmp" },
  };

  const mockBot: any = {
    sendMessage: mock(async () => {}),
    sendChunkedMessage: mock(async () => {}),
    bot: {},
  };

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);
  });

  afterEach(() => {
    db.close();
  });

  test("creates orchestrator with registerTool method", () => {
    const orch = createOrchestrator(config, mockBot);
    expect(typeof orch.registerTool).toBe("function");
    expect(typeof orch.handleMessage).toBe("function");
    expect(typeof orch.runPrompt).toBe("function");
  });

  test("registerTool adds a tool handler", () => {
    const orch = createOrchestrator(config, mockBot);
    const handler = mock(async () => "result");
    orch.registerTool("test_tool", handler);
  });

  test("session management - saves and retrieves", () => {
    db.prepare(
      "INSERT INTO sessions (id, history) VALUES (?, ?)"
    ).run("test-session", JSON.stringify([{ role: "user", content: "hello" }]));

    const row = db.query("SELECT * FROM sessions WHERE id = 'test-session'").get() as any;
    expect(row).toBeTruthy();
    const history = JSON.parse(row.history);
    expect(history).toHaveLength(1);
    expect(history[0].content).toBe("hello");
  });

  test("session management - upsert works", () => {
    db.prepare(
      `INSERT INTO sessions (id, history, updated_at) VALUES (?, ?, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET history = excluded.history, updated_at = excluded.updated_at`
    ).run("upsert-test", "[]");

    db.prepare(
      `INSERT INTO sessions (id, history, updated_at) VALUES (?, ?, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET history = excluded.history, updated_at = excluded.updated_at`
    ).run("upsert-test", '[{"role":"user","content":"updated"}]');

    const row = db.query("SELECT * FROM sessions WHERE id = 'upsert-test'").get() as any;
    const history = JSON.parse(row.history);
    expect(history).toHaveLength(1);
    expect(history[0].content).toBe("updated");
  });
});
