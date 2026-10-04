import { describe, test, expect, beforeEach, afterEach, mock } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { handleCommand } from "../../src/telegram/commands.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("commands", () => {
  let db: Database;
  let sentMessages: Array<{ chatId: number | string; text: string; parseMode?: string }>;

  const mockBot: any = {
    sendMessage: mock(async (chatId: number | string, text: string, parseMode?: string) => {
      sentMessages.push({ chatId, text, parseMode });
    }),
    sendChunkedMessage: mock(async (chatId: number | string, text: string, parseMode?: string) => {
      sentMessages.push({ chatId, text, parseMode });
    }),
  };

  const config: any = {
    telegram: { botToken: "test", allowedUsers: [123], allowedUsernames: [] },
    anthropic: { apiKey: "test", model: "test", maxTokens: 1000 },
    openai: { apiKey: "test", embeddingModel: "test" },
    workspace: { dir: "/tmp", memoryDir: "/tmp/mem" },
    daemon: { logLevel: "info", dataDir: "/tmp" },
  };

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);
    sentMessages = [];
  });

  afterEach(() => {
    db.close();
  });

  function makeCtx(text: string, chatId = 123) {
    return {
      message: { text },
      chat: { id: chatId },
      api: { getMe: async () => ({ username: "test_bot" }) },
    } as any;
  }

  test("/status returns system info", async () => {
    const handled = await handleCommand(makeCtx("/status"), config, mockBot);
    expect(handled).toBe(true);
    expect(sentMessages.length).toBe(1);
    expect(sentMessages[0]!.text).toContain("smolclaw status");
  });

  test("/reset clears session", async () => {
    db.prepare("INSERT INTO sessions (id, history) VALUES (?, ?)").run("123", '[{"role":"user","content":"hi"}]');
    const handled = await handleCommand(makeCtx("/reset"), config, mockBot);
    expect(handled).toBe(true);
    const session = db.query("SELECT * FROM sessions WHERE id = '123'").get();
    expect(session).toBeNull();
  });

  test("/help returns help text", async () => {
    const handled = await handleCommand(makeCtx("/help"), config, mockBot);
    expect(handled).toBe(true);
    expect(sentMessages[0]!.text).toContain("Commands:");
  });

  test("/ps with no processes", async () => {
    const handled = await handleCommand(makeCtx("/ps"), config, mockBot);
    expect(handled).toBe(true);
    expect(sentMessages[0]!.text).toContain("No managed processes");
  });

  test("/cron with no jobs", async () => {
    const handled = await handleCommand(makeCtx("/cron"), config, mockBot);
    expect(handled).toBe(true);
    expect(sentMessages[0]!.text).toContain("No cron jobs");
  });

  test("/loops with no loops", async () => {
    const handled = await handleCommand(makeCtx("/loops"), config, mockBot);
    expect(handled).toBe(true);
    expect(sentMessages[0]!.text).toContain("No learning loops");
  });

  test("unrecognized command returns false", async () => {
    const handled = await handleCommand(makeCtx("/nonexistent"), config, mockBot);
    expect(handled).toBe(false);
  });
});
