import { Cron } from "croner";
import { v4 as uuidv4 } from "uuid";
import type { Config } from "../config.ts";
import type { Orchestrator } from "../agent/orchestrator.ts";
import type { TelegramBot } from "../telegram/bot.ts";
import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { markdownToTelegramHtml } from "../telegram/formatting.ts";
import type { LoopRecord } from "./types.ts";

export interface LoopManager {
  create(
    name: string,
    reviewCron: string,
    reviewPrompt: string,
    targetProcess: string | null,
    applyMode: string,
    description?: string
  ): string;
  pause(name: string): string;
  resume(name: string): string;
  status(name: string): string;
  list(): string;
  history(name: string): string;
  startAll(): void;
  stopAll(): void;
}

export function createLoopManager(
  config: Config,
  orchestrator: Orchestrator,
  bot: TelegramBot
): LoopManager {
  const log = getLogger();
  const activeCrons = new Map<string, Cron>();

  function getLoop(name: string): LoopRecord | null {
    const db = getDatabase();
    return db.query<LoopRecord, [string]>("SELECT * FROM loops WHERE name = ?").get(name) ?? null;
  }

  async function runCycle(loop: LoopRecord) {
    const log = getLogger();
    const db = getDatabase();
    const ownerChatId = config.telegram.allowedUsers[0];

    log.info({ loopName: loop.name, cycle: loop.cycle_count + 1 }, "Running loop cycle");

    const sessionId = `loop:${loop.id}`;

    // Build context-aware prompt including previous cycle data
    let prompt = loop.review_prompt;
    if (loop.last_cycle_result) {
      prompt += `\n\nPrevious cycle result:\n${loop.last_cycle_result}`;
    }
    if (loop.target_process) {
      prompt += `\n\nTarget process: ${loop.target_process}. Check its logs and status.`;
    }
    prompt += `\n\nThis is cycle ${loop.cycle_count + 1}. Save important findings to memory with memory_write.`;

    if (loop.apply_mode === "suggest") {
      prompt += `\nPresent your recommendations but do NOT auto-apply changes. Wait for user approval.`;
    } else {
      prompt += `\nYou may apply changes directly if confident they will improve things.`;
    }

    try {
      const result = await orchestrator.runPrompt(sessionId, prompt);

      // Update loop state
      db.prepare(
        `UPDATE loops SET
          cycle_count = cycle_count + 1,
          last_cycle_at = datetime('now'),
          last_cycle_result = ?
        WHERE name = ?`
      ).run(result.slice(0, 5000), loop.name);

      // Notify owner
      if (ownerChatId) {
        const html = markdownToTelegramHtml(result);
        const prefix = `<b>[Loop: ${loop.name} - Cycle ${loop.cycle_count + 1}]</b>\n`;
        await bot.sendChunkedMessage(ownerChatId, prefix + html, "HTML");
      }

      log.info({ loopName: loop.name, cycle: loop.cycle_count + 1 }, "Loop cycle completed");
    } catch (err) {
      log.error({ err, loopName: loop.name }, "Loop cycle failed");
      if (ownerChatId) {
        const errMsg = err instanceof Error ? err.message : String(err);
        await bot.sendMessage(
          ownerChatId,
          `<b>[Loop Error: ${loop.name}]</b> ${errMsg}`,
          "HTML"
        );
      }
    }
  }

  function startLoop(loop: LoopRecord) {
    if (activeCrons.has(loop.name)) {
      activeCrons.get(loop.name)!.stop();
    }

    const cron = new Cron(loop.review_cron, async () => {
      const current = getLoop(loop.name);
      if (!current || current.status !== "active") {
        log.info({ loopName: loop.name }, "Loop no longer active, stopping cron");
        activeCrons.get(loop.name)?.stop();
        activeCrons.delete(loop.name);
        return;
      }
      await runCycle(current);
    });

    activeCrons.set(loop.name, cron);
    log.info({ loopName: loop.name, cron: loop.review_cron }, "Loop scheduled");
  }

  return {
    create(name, reviewCron, reviewPrompt, targetProcess, applyMode, description) {
      const db = getDatabase();
      const existing = getLoop(name);
      if (existing) return `Loop "${name}" already exists.`;

      try {
        new Cron(reviewCron, { paused: true });
      } catch (err) {
        return `Invalid cron expression: ${err instanceof Error ? err.message : String(err)}`;
      }

      const id = uuidv4();
      db.prepare(
        `INSERT INTO loops (id, name, description, target_process, review_cron, review_prompt, apply_mode)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      ).run(id, name, description ?? null, targetProcess, reviewCron, reviewPrompt, applyMode);

      const loop = getLoop(name)!;
      startLoop(loop);

      return `Loop "${name}" created and scheduled (${reviewCron}, ${applyMode} mode)`;
    },

    pause(name) {
      const db = getDatabase();
      db.prepare("UPDATE loops SET status = 'paused' WHERE name = ?").run(name);

      const cron = activeCrons.get(name);
      if (cron) {
        cron.stop();
        activeCrons.delete(name);
      }

      return `Loop "${name}" paused`;
    },

    resume(name) {
      const db = getDatabase();
      db.prepare("UPDATE loops SET status = 'active' WHERE name = ?").run(name);

      const loop = getLoop(name);
      if (loop) {
        startLoop(loop);
        return `Loop "${name}" resumed`;
      }
      return `Loop "${name}" not found`;
    },

    status(name) {
      const loop = getLoop(name);
      if (!loop) return `Loop "${name}" not found`;

      const cron = activeCrons.get(name);
      const nextRun = cron?.nextRun()?.toISOString() ?? "N/A";

      return [
        `Name: ${loop.name}`,
        `Status: ${loop.status}`,
        `Description: ${loop.description ?? "N/A"}`,
        `Target Process: ${loop.target_process ?? "none"}`,
        `Schedule: ${loop.review_cron}`,
        `Apply Mode: ${loop.apply_mode}`,
        `Cycles: ${loop.cycle_count}`,
        `Last Cycle: ${loop.last_cycle_at ?? "never"}`,
        `Next Run: ${nextRun}`,
        loop.last_cycle_result
          ? `\nLast Result:\n${loop.last_cycle_result.slice(0, 500)}`
          : "",
      ]
        .filter(Boolean)
        .join("\n");
    },

    list() {
      const db = getDatabase();
      const loops = db.query<LoopRecord, []>("SELECT * FROM loops ORDER BY name").all();

      if (loops.length === 0) return "No learning loops.";

      return loops
        .map((l) => {
          const cron = activeCrons.get(l.name);
          const nextRun = cron?.nextRun()?.toISOString() ?? "N/A";
          return `${l.name} [${l.status}] (${l.apply_mode})\n  Schedule: ${l.review_cron} | Cycles: ${l.cycle_count}\n  Next: ${nextRun}`;
        })
        .join("\n\n");
    },

    history(name) {
      const loop = getLoop(name);
      if (!loop) return `Loop "${name}" not found`;

      // History is stored in the session associated with this loop
      const db = getDatabase();
      const session = db
        .query<{ history: string }, [string]>("SELECT history FROM sessions WHERE id = ?")
        .get(`loop:${loop.id}`);

      if (!session) return `No history for loop "${name}"`;

      try {
        const history = JSON.parse(session.history) as Array<{ role: string; content: string }>;
        const recentAssistant = history
          .filter((h) => h.role === "assistant")
          .slice(-5)
          .map((h, i) => `--- Cycle ${i + 1} ---\n${h.content.slice(0, 500)}`)
          .join("\n\n");

        return recentAssistant || "No cycle results yet";
      } catch {
        return "Failed to parse loop history";
      }
    },

    startAll() {
      const db = getDatabase();
      const loops = db.query<LoopRecord, []>("SELECT * FROM loops WHERE status = 'active'").all();

      for (const loop of loops) {
        startLoop(loop);
      }

      log.info({ count: loops.length }, "Started all active loops");
    },

    stopAll() {
      for (const [name, cron] of activeCrons) {
        cron.stop();
        log.info({ name }, "Stopped loop");
      }
      activeCrons.clear();
    },
  };
}
