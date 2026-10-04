import { describe, test, expect, beforeEach, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import { setLogger } from "../../src/logger.ts";
import { setDatabase } from "../../src/db/connection.ts";
import { runMigrations } from "../../src/db/migrations.ts";
import { createProcessManager } from "../../src/process/manager.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

const config: any = {
  workspace: { dir: "/tmp" },
};

describe("process manager", () => {
  let db: Database;
  let pm: ReturnType<typeof createProcessManager>;

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec("PRAGMA journal_mode = WAL");
    setDatabase(db);
    runMigrations(db);
    pm = createProcessManager(config);
  });

  afterEach(async () => {
    const records = db.query<{ name: string }, []>("SELECT name FROM processes").all();
    for (const r of records) {
      try {
        await pm.stop(r.name);
      } catch {
        // ignore
      }
    }
    db.close();
  });

  test("start creates a process", async () => {
    const result = await pm.start("test-echo", "echo hello && sleep 1", "/tmp");
    expect(result).toContain("started");
    expect(result).toContain("PID");

    const record = db.query("SELECT * FROM processes WHERE name = 'test-echo'").get() as any;
    expect(record).toBeTruthy();
    expect(record.status).toBe("running");
  });

  test("start duplicate returns already running", async () => {
    await pm.start("dup-test", "sleep 10", "/tmp");
    const result = await pm.start("dup-test", "sleep 10", "/tmp");
    expect(result).toContain("already running");
    await pm.stop("dup-test");
  });

  test("stop terminates process", async () => {
    await pm.start("stop-test", "sleep 60", "/tmp");
    const result = await pm.stop("stop-test");
    expect(result).toContain("stopped");
  });

  test("stop non-existent returns not running", async () => {
    const result = await pm.stop("nonexistent");
    expect(result).toContain("not running");
  });

  test("status returns process info", async () => {
    await pm.start("status-test", "sleep 30", "/tmp");
    const result = await pm.status("status-test");
    expect(result).toContain("Name: status-test");
    expect(result).toContain("Command: sleep 30");
    await pm.stop("status-test");
  });

  test("status for missing process", async () => {
    const result = await pm.status("missing");
    expect(result).toContain("not found");
  });

  test("list with no processes", () => {
    const result = pm.list();
    expect(result).toContain("No managed processes");
  });

  test("list shows processes", async () => {
    await pm.start("list-test", "sleep 30", "/tmp");
    const result = pm.list();
    expect(result).toContain("list-test");
    expect(result).toContain("running");
    await pm.stop("list-test");
  });

  test("logs returns string", async () => {
    await pm.start("log-test", "echo 'hello from process' && sleep 1", "/tmp");
    await Bun.sleep(500);
    const logs = pm.logs("log-test", 10);
    expect(typeof logs).toBe("string");
    await pm.stop("log-test");
  });
});
