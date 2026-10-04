import { loadConfig } from "./config.ts";
import { initLogger, getLogger } from "./logger.ts";
import { initDatabase } from "./db/connection.ts";
import { runMigrations } from "./db/migrations.ts";
import { createBot } from "./telegram/bot.ts";
import { createOrchestrator } from "./agent/orchestrator.ts";
import { initShutdownHandlers, onShutdown } from "./utils/shutdown.ts";
import { isAuthenticated, login } from "./auth.ts";
import { mkdirSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { Glob } from "bun";

async function main() {
  // 1. Load config
  const config = loadConfig();

  // 2. Ensure data dirs exist
  if (!existsSync(config.daemon.dataDir)) {
    mkdirSync(config.daemon.dataDir, { recursive: true });
  }
  if (!existsSync(config.workspace.memoryDir)) {
    mkdirSync(config.workspace.memoryDir, { recursive: true });
  }

  // 3. Init logger
  const log = initLogger(config);
  log.info("smolclaw starting...");

  // 4. Authenticate — if no API key in config, use OAuth (same as Claude Code)
  const forceReauth = process.argv.includes("--reauth");
  if (!config.anthropic.apiKey) {
    if (forceReauth || !isAuthenticated()) {
      console.log("╔══════════════════════════════════════╗");
      console.log("║           smolclaw Setup             ║");
      console.log("╚══════════════════════════════════════╝");
      console.log("");
      console.log("No API key found. Logging in with Claude...");
      console.log("Press Enter to open the browser and authenticate.");

      // Wait for Enter key
      process.stdin.setRawMode?.(false);
      await new Promise<void>((resolve) => {
        process.stdin.once("data", () => resolve());
        process.stdin.resume();
      });

      await login();
    } else {
      console.log("Authenticated with Claude (OAuth)");
    }
  }

  // 4. Init database
  const dbPath = `${config.daemon.dataDir}/data.db`;
  const db = initDatabase(dbPath);
  runMigrations(db);
  console.log("Database initialized");

  // 5. Create orchestrator (needs bot reference, will be wired below)
  let orchestratorRef: ReturnType<typeof createOrchestrator>;

  // 6. Create Telegram bot
  const bot = createBot(config, async (ctx) => {
    await orchestratorRef.handleMessage(ctx);
  });

  // 7. Wire orchestrator
  orchestratorRef = createOrchestrator(config, bot);

  // 8. Register system tools (bash, read_file, write_file, list_files)
  registerSystemTools(config, orchestratorRef);

  // 9. Register subsystem tool handlers
  await registerSubsystems(config, orchestratorRef, bot);

  // 9. Graceful shutdown
  initShutdownHandlers();
  onShutdown(() => bot.stop());

  // 10. Start bot
  console.log("Starting Telegram bot...");
  await bot.start();
  console.log("smolclaw is running. Send a message to your bot on Telegram.");
}

function registerSystemTools(
  config: ReturnType<typeof loadConfig>,
  orchestrator: ReturnType<typeof createOrchestrator>
) {
  const log = getLogger();

  // bash — execute shell commands
  orchestrator.registerTool("bash", async (input) => {
    const command = input.command as string;
    const cwd = (input.cwd as string) ?? config.workspace.dir;
    const timeoutMs = (input.timeout_ms as number) ?? 60_000;

    const proc = Bun.spawn(["bash", "-c", command], {
      cwd,
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env, HOME: config.workspace.dir },
    });

    const timeout = setTimeout(() => proc.kill(), timeoutMs);
    let stdout = "";
    let stderr = "";

    try {
      stdout = await new Response(proc.stdout).text();
      stderr = await new Response(proc.stderr).text();
      await proc.exited;
    } finally {
      clearTimeout(timeout);
    }

    const exitCode = proc.exitCode ?? -1;
    const parts: string[] = [];
    if (stdout.trim()) parts.push(stdout.trim());
    if (stderr.trim()) parts.push(`STDERR:\n${stderr.trim()}`);
    if (exitCode !== 0) parts.push(`Exit code: ${exitCode}`);
    return parts.join("\n") || "(no output)";
  });

  // read_file — read file contents
  orchestrator.registerTool("read_file", async (input) => {
    const path = resolve(config.workspace.dir, input.path as string);
    const maxLines = input.max_lines as number | undefined;

    try {
      const content = readFileSync(path, "utf-8");
      if (maxLines && maxLines > 0) {
        return content.split("\n").slice(0, maxLines).join("\n");
      }
      // Cap at 100k chars to avoid blowing up context
      if (content.length > 100_000) {
        return content.slice(0, 100_000) + "\n... (truncated at 100k chars)";
      }
      return content;
    } catch (err) {
      return `Error reading file: ${err instanceof Error ? err.message : String(err)}`;
    }
  });

  // write_file — write file contents
  orchestrator.registerTool("write_file", async (input) => {
    const path = resolve(config.workspace.dir, input.path as string);
    const content = input.content as string;

    try {
      const dir = dirname(path);
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
      writeFileSync(path, content, "utf-8");
      return `Written ${content.length} bytes to ${path}`;
    } catch (err) {
      return `Error writing file: ${err instanceof Error ? err.message : String(err)}`;
    }
  });

  // list_files — glob for files
  orchestrator.registerTool("list_files", async (input) => {
    const pattern = input.pattern as string;
    const cwd = (input.cwd as string) ?? config.workspace.dir;

    try {
      const glob = new Glob(pattern);
      const files: string[] = [];
      for await (const file of glob.scan({ cwd, dot: false })) {
        files.push(file);
        if (files.length >= 500) {
          files.push("... (truncated at 500 results)");
          break;
        }
      }
      return files.length > 0 ? files.join("\n") : "No files matched.";
    } catch (err) {
      return `Error listing files: ${err instanceof Error ? err.message : String(err)}`;
    }
  });

  // web_search — search the web via DuckDuckGo
  orchestrator.registerTool("web_search", async (input) => {
    const { webSearch } = await import("./web/search.ts");
    const query = input.query as string;
    const maxResults = (input.max_results as number) ?? 5;
    return await webSearch(query, maxResults);
  });

  // web_fetch — fetch a URL and extract readable text
  orchestrator.registerTool("web_fetch", async (input) => {
    const { webFetch } = await import("./web/fetch.ts");
    const url = input.url as string;
    const maxLength = (input.max_length as number) ?? 20_000;
    return await webFetch(url, maxLength);
  });

  log.info("System tools registered (bash, read_file, write_file, list_files, web_search, web_fetch)");
  console.log("System tools registered");
}

async function registerSubsystems(
  config: ReturnType<typeof loadConfig>,
  orchestrator: ReturnType<typeof createOrchestrator>,
  bot: ReturnType<typeof createBot>
) {
  const log = getLogger();

  // Memory tools
  try {
    const { addMemory } = await import("./memory/store.ts");
    const { searchMemory } = await import("./memory/search.ts");

    orchestrator.registerTool("memory_write", async (input) => {
      const content = input.content as string;
      const tags = (input.tags as string[]) ?? [];
      const source = (input.source as string) ?? "conversation";
      const id = await addMemory(content, source, undefined, tags);
      return `Memory saved (id: ${id})`;
    });

    orchestrator.registerTool("memory_search", async (input) => {
      const query = input.query as string;
      const limit = (input.limit as number) ?? 10;
      const results = await searchMemory(query, limit);
      if (results.length === 0) return "No memories found.";
      return results
        .map(
          (r, i) =>
            `${i + 1}. [${r.source}] ${r.content.slice(0, 300)}${r.content.length > 300 ? "..." : ""}`
        )
        .join("\n\n");
    });
    log.info("Memory tools registered");
  } catch (err) {
    log.warn({ err }, "Memory subsystem not available yet");
  }

  // Process management tool
  try {
    const { createProcessManager } = await import("./process/manager.ts");
    const pm = createProcessManager(config);

    orchestrator.registerTool("process_manage", async (input) => {
      const action = input.action as string;
      switch (action) {
        case "start":
          return await pm.start(
            input.name as string,
            input.command as string,
            (input.cwd as string) ?? config.workspace.dir,
            (input.restart_policy as string) ?? "none"
          );
        case "stop":
          return await pm.stop(input.name as string);
        case "restart":
          return await pm.restart(input.name as string);
        case "status":
          return await pm.status(input.name as string);
        case "logs":
          return pm.logs(input.name as string, (input.lines as number) ?? 50);
        case "list":
          return pm.list();
        default:
          return `Unknown action: ${action}`;
      }
    });
    log.info("Process management tool registered");
  } catch (err) {
    log.warn({ err }, "Process subsystem not available yet");
  }

  // Cron management tool
  try {
    const { createScheduler } = await import("./cron/scheduler.ts");
    const scheduler = createScheduler(config, orchestrator, bot);

    orchestrator.registerTool("cron_manage", async (input) => {
      const action = input.action as string;
      switch (action) {
        case "create":
          return scheduler.create(
            input.name as string,
            input.cron_expr as string,
            input.action_type as string,
            input.action_payload as string
          );
        case "delete":
          return scheduler.remove(input.name as string);
        case "list":
          return scheduler.list();
        case "enable":
          return scheduler.enable(input.name as string);
        case "disable":
          return scheduler.disable(input.name as string);
        case "trigger":
          return await scheduler.trigger(input.name as string);
        default:
          return `Unknown action: ${action}`;
      }
    });

    // Start all enabled cron jobs
    scheduler.startAll();
    log.info("Cron subsystem registered and started");

    onShutdown(() => scheduler.stopAll());
  } catch (err) {
    log.warn({ err }, "Cron subsystem not available yet");
  }

  // Loop management tool
  try {
    const { createLoopManager } = await import("./loops/manager.ts");
    const lm = createLoopManager(config, orchestrator, bot);

    orchestrator.registerTool("loop_manage", async (input) => {
      const action = input.action as string;
      switch (action) {
        case "create":
          return lm.create(
            input.name as string,
            input.review_cron as string,
            input.review_prompt as string,
            (input.target_process as string) ?? null,
            (input.apply_mode as string) ?? "suggest",
            (input as any).description as string | undefined
          );
        case "pause":
          return lm.pause(input.name as string);
        case "resume":
          return lm.resume(input.name as string);
        case "status":
          return lm.status(input.name as string);
        case "list":
          return lm.list();
        case "history":
          return lm.history(input.name as string);
        default:
          return `Unknown action: ${action}`;
      }
    });

    lm.startAll();
    log.info("Loop subsystem registered and started");

    onShutdown(() => lm.stopAll());
  } catch (err) {
    log.warn({ err }, "Loop subsystem not available yet");
  }

  // File watcher for memory directory
  try {
    const { startMemorySync } = await import("./memory/sync.ts");
    startMemorySync(config);
    log.info("Memory file sync started");
  } catch (err) {
    log.warn({ err }, "Memory file sync not available yet");
  }
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
