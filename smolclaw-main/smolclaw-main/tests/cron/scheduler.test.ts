import { describe, test, expect, beforeEach, afterEach, mock } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { createScheduler } from "../../src/cron/scheduler.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("cron scheduler", () => {
  let db: Database;
  let scheduler: ReturnType<typeof createScheduler>;

  const config: any = {
    workspace: { dir: "/tmp" },
    telegram: { botToken: "test", allowedUsers: [123], allowedUsernames: [] },
  };

  const mockOrchestrator: any = {
    runPrompt: mock(async () => "Agent response"),
  };

  const mockBot: any = {
    sendMessage: mock(async () => {}),
    sendChunkedMessage: mock(async () => {}),
  };

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);
    scheduler = createScheduler(config, mockOrchestrator, mockBot);
  });

  afterEach(() => {
    scheduler.stopAll();
    db.close();
  });

  test("create adds a cron job", () => {
    const result = scheduler.create("test-job", "*/5 * * * *", "shell", '{"command":"echo hi"}');
    expect(result).toContain("created");

    const job = db.query("SELECT * FROM cron_jobs WHERE name = 'test-job'").get() as any;
    expect(job).toBeTruthy();
    expect(job.cron_expr).toBe("*/5 * * * *");
  });

  test("create rejects duplicate", () => {
    scheduler.create("dup-job", "* * * * *", "shell", '{"command":"echo"}');
    const result = scheduler.create("dup-job", "* * * * *", "shell", '{"command":"echo"}');
    expect(result).toContain("already exists");
  });

  test("create rejects invalid cron", () => {
    const result = scheduler.create("bad-cron", "not-valid", "shell", '{}');
    expect(result).toContain("Invalid cron");
  });

  test("remove deletes a job", () => {
    scheduler.create("remove-job", "0 * * * *", "agent", '{"prompt":"test"}');
    const result = scheduler.remove("remove-job");
    expect(result).toContain("deleted");
  });

  test("remove non-existent returns not found", () => {
    const result = scheduler.remove("missing-job");
    expect(result).toContain("not found");
  });

  test("list with no jobs", () => {
    const result = scheduler.list();
    expect(result).toContain("No cron jobs");
  });

  test("list shows jobs", () => {
    scheduler.create("list-job", "0 * * * *", "shell", '{"command":"date"}');
    const result = scheduler.list();
    expect(result).toContain("list-job");
    expect(result).toContain("shell");
  });

  test("enable/disable toggles job state", () => {
    scheduler.create("toggle-job", "0 * * * *", "shell", '{"command":"echo"}');

    scheduler.disable("toggle-job");
    let job = db.query("SELECT * FROM cron_jobs WHERE name = 'toggle-job'").get() as any;
    expect(job.enabled).toBe(0);

    scheduler.enable("toggle-job");
    job = db.query("SELECT * FROM cron_jobs WHERE name = 'toggle-job'").get() as any;
    expect(job.enabled).toBe(1);
  });

  test("trigger runs job immediately", async () => {
    scheduler.create("trigger-job", "0 0 1 1 *", "shell", '{"command":"echo triggered"}');
    const result = await scheduler.trigger("trigger-job");
    expect(result).toContain("Triggered");
  });

  test("trigger non-existent returns not found", async () => {
    const result = await scheduler.trigger("missing");
    expect(result).toContain("not found");
  });
});
