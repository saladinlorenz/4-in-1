import { Cron } from "croner";
import { v4 as uuidv4 } from "uuid";
import type { Config } from "../config.ts";
import type { Orchestrator } from "../agent/orchestrator.ts";
import type { TelegramBot } from "../telegram/bot.ts";
import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { executeJob } from "./executor.ts";

interface CronJobRecord {
  id: string;
  name: string;
  cron_expr: string;
  action_type: string;
  action_payload: string;
  enabled: number;
  last_run_at: string | null;
  last_result: string | null;
  created_at: string;
}

export interface Scheduler {
  create(name: string, cronExpr: string, actionType: string, actionPayload: string): string;
  remove(name: string): string;
  list(): string;
  enable(name: string): string;
  disable(name: string): string;
  trigger(name: string): Promise<string>;
  startAll(): void;
  stopAll(): void;
}

export function createScheduler(
  config: Config,
  orchestrator: Orchestrator,
  bot: TelegramBot
): Scheduler {
  const log = getLogger();
  const activeCrons = new Map<string, Cron>();

  function getJob(name: string): CronJobRecord | null {
    const db = getDatabase();
    return db.query<CronJobRecord, [string]>("SELECT * FROM cron_jobs WHERE name = ?").get(name) ?? null;
  }

  function startCronJob(job: CronJobRecord) {
    if (activeCrons.has(job.name)) {
      activeCrons.get(job.name)!.stop();
    }

    const cron = new Cron(job.cron_expr, async () => {
      log.info({ jobName: job.name, actionType: job.action_type }, "Cron job firing");

      const result = await executeJob(job.id, job.action_type, job.action_payload, config, orchestrator, bot);

      const db = getDatabase();
      db.prepare(
        "UPDATE cron_jobs SET last_run_at = datetime('now'), last_result = ? WHERE name = ?"
      ).run(JSON.stringify(result), job.name);

      log.info({ jobName: job.name, success: result.success, duration: result.duration_ms }, "Cron job completed");
    });

    activeCrons.set(job.name, cron);
    log.info({ jobName: job.name, cronExpr: job.cron_expr }, "Cron job scheduled");
  }

  return {
    create(name, cronExpr, actionType, actionPayload) {
      const db = getDatabase();
      const existing = getJob(name);
      if (existing) return `Cron job "${name}" already exists. Delete it first.`;

      // Validate cron expression
      try {
        new Cron(cronExpr, { paused: true });
      } catch (err) {
        return `Invalid cron expression: ${err instanceof Error ? err.message : String(err)}`;
      }

      const id = uuidv4();
      db.prepare(
        "INSERT INTO cron_jobs (id, name, cron_expr, action_type, action_payload) VALUES (?, ?, ?, ?, ?)"
      ).run(id, name, cronExpr, actionType, actionPayload);

      const job = getJob(name)!;
      startCronJob(job);

      return `Cron job "${name}" created and scheduled (${cronExpr}, ${actionType})`;
    },

    remove(name) {
      const cron = activeCrons.get(name);
      if (cron) {
        cron.stop();
        activeCrons.delete(name);
      }

      const db = getDatabase();
      const result = db.prepare("DELETE FROM cron_jobs WHERE name = ?").run(name);
      return result.changes > 0 ? `Cron job "${name}" deleted` : `Cron job "${name}" not found`;
    },

    list() {
      const db = getDatabase();
      const jobs = db.query<CronJobRecord, []>("SELECT * FROM cron_jobs ORDER BY name").all();

      if (jobs.length === 0) return "No cron jobs.";

      return jobs
        .map((j) => {
          const status = j.enabled ? "enabled" : "disabled";
          const lastRun = j.last_run_at ?? "never";
          const cron = activeCrons.get(j.name);
          const nextRun = cron?.nextRun()?.toISOString() ?? "N/A";
          return `${j.name} [${j.action_type}] (${status})\n  Schedule: ${j.cron_expr}\n  Last: ${lastRun} | Next: ${nextRun}`;
        })
        .join("\n\n");
    },

    enable(name) {
      const db = getDatabase();
      db.prepare("UPDATE cron_jobs SET enabled = 1 WHERE name = ?").run(name);

      const job = getJob(name);
      if (job) {
        startCronJob(job);
        return `Cron job "${name}" enabled`;
      }
      return `Cron job "${name}" not found`;
    },

    disable(name) {
      const db = getDatabase();
      db.prepare("UPDATE cron_jobs SET enabled = 0 WHERE name = ?").run(name);

      const cron = activeCrons.get(name);
      if (cron) {
        cron.stop();
        activeCrons.delete(name);
      }

      return `Cron job "${name}" disabled`;
    },

    async trigger(name) {
      const job = getJob(name);
      if (!job) return `Cron job "${name}" not found`;

      const result = await executeJob(job.id, job.action_type, job.action_payload, config, orchestrator, bot);

      const db = getDatabase();
      db.prepare(
        "UPDATE cron_jobs SET last_run_at = datetime('now'), last_result = ? WHERE name = ?"
      ).run(JSON.stringify(result), name);

      return `Triggered "${name}": ${result.success ? "success" : "failed"} (${result.duration_ms}ms)\n${result.output.slice(0, 500)}`;
    },

    startAll() {
      const db = getDatabase();
      const jobs = db.query<CronJobRecord, []>("SELECT * FROM cron_jobs WHERE enabled = 1").all();

      for (const job of jobs) {
        startCronJob(job);
      }

      log.info({ count: jobs.length }, "Started all enabled cron jobs");
    },

    stopAll() {
      for (const [name, cron] of activeCrons) {
        cron.stop();
        log.info({ name }, "Stopped cron job");
      }
      activeCrons.clear();
    },
  };
}
