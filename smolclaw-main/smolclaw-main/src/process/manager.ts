import type { Subprocess } from "bun";
import { v4 as uuidv4 } from "uuid";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";
import { LogTail } from "./log-tail.ts";
import {
  getProcessRecord,
  upsertProcess,
  updateProcessStatus,
  incrementRestartCount,
  listProcessRecords,
} from "./registry.ts";

interface RunningProcess {
  proc: Subprocess;
  logs: LogTail;
  name: string;
}

export interface ProcessManager {
  start(name: string, command: string, cwd: string, restartPolicy?: string): Promise<string>;
  stop(name: string): Promise<string>;
  restart(name: string): Promise<string>;
  status(name: string): Promise<string>;
  logs(name: string, lines?: number): string;
  list(): string;
}

export function createProcessManager(config: Config): ProcessManager {
  const log = getLogger();
  const running = new Map<string, RunningProcess>();

  function spawnProcess(name: string, command: string, cwd: string): Subprocess {
    const proc = Bun.spawn(["sh", "-c", command], {
      cwd,
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });

    const rp = running.get(name);
    if (rp) {
      // Read stdout
      if (proc.stdout) {
        const reader = proc.stdout.getReader();
        const decoder = new TextDecoder();
        (async () => {
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              const text = decoder.decode(value);
              rp.logs.pushChunk(text);
            }
          } catch {
            // Process ended
          }
        })();
      }

      // Read stderr
      if (proc.stderr) {
        const reader = proc.stderr.getReader();
        const decoder = new TextDecoder();
        (async () => {
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              const text = decoder.decode(value);
              rp.logs.pushChunk(`[stderr] ${text}`);
            }
          } catch {
            // Process ended
          }
        })();
      }
    }

    // Monitor for exit
    proc.exited.then((exitCode) => {
      log.info({ name, exitCode, pid: proc.pid }, "Process exited");
      updateProcessStatus(name, exitCode === 0 ? "stopped" : "crashed", null, exitCode);

      const record = getProcessRecord(name);
      if (!record) return;

      // Auto-restart logic
      if (
        exitCode !== 0 &&
        (record.restart_policy === "on-crash" || record.restart_policy === "always") &&
        record.restart_count < record.max_restarts
      ) {
        const count = incrementRestartCount(name);
        log.info({ name, restartCount: count, maxRestarts: record.max_restarts }, "Auto-restarting process");

        setTimeout(() => {
          try {
            const newProc = spawnProcess(name, record.command, record.cwd ?? config.workspace.dir);
            const rp = running.get(name);
            if (rp) {
              rp.proc = newProc;
            }
            updateProcessStatus(name, "running", newProc.pid);
          } catch (err) {
            log.error({ err, name }, "Failed to restart process");
            updateProcessStatus(name, "crashed");
          }
        }, 2000); // 2s delay between restarts
      } else if (record.restart_policy === "always" && exitCode === 0) {
        const count = incrementRestartCount(name);
        log.info({ name, restartCount: count }, "Restarting (always policy)");
        setTimeout(() => {
          try {
            const newProc = spawnProcess(name, record.command, record.cwd ?? config.workspace.dir);
            const rp = running.get(name);
            if (rp) rp.proc = newProc;
            updateProcessStatus(name, "running", newProc.pid);
          } catch (err) {
            log.error({ err, name }, "Failed to restart process");
          }
        }, 1000);
      }
    });

    return proc;
  }

  return {
    async start(name, command, cwd, restartPolicy = "none") {
      if (running.has(name)) {
        const rp = running.get(name)!;
        if (rp.proc.exitCode === null) {
          return `Process "${name}" is already running (PID ${rp.proc.pid})`;
        }
      }

      const id = getProcessRecord(name)?.id ?? uuidv4();

      // Create log tail and running entry first so stream readers can access it
      const logTail = new LogTail(1000);
      const rp: RunningProcess = { proc: null as any, logs: logTail, name };
      running.set(name, rp);

      const proc = spawnProcess(name, command, cwd);
      rp.proc = proc;

      upsertProcess({
        id,
        name,
        command,
        cwd,
        status: "running",
        pid: proc.pid,
        restart_policy: restartPolicy,
        restart_count: 0,
      });

      log.info({ name, pid: proc.pid, command, cwd }, "Process started");
      return `Process "${name}" started (PID ${proc.pid})`;
    },

    async stop(name) {
      const rp = running.get(name);
      if (!rp || rp.proc.exitCode !== null) {
        updateProcessStatus(name, "stopped");
        running.delete(name);
        return `Process "${name}" is not running`;
      }

      rp.proc.kill();
      await rp.proc.exited;
      running.delete(name);
      updateProcessStatus(name, "stopped", null, rp.proc.exitCode);

      return `Process "${name}" stopped`;
    },

    async restart(name) {
      const record = getProcessRecord(name);
      if (!record) return `Process "${name}" not found`;

      await this.stop(name);
      await Bun.sleep(500);
      return await this.start(name, record.command, record.cwd ?? config.workspace.dir, record.restart_policy);
    },

    async status(name) {
      const record = getProcessRecord(name);
      if (!record) return `Process "${name}" not found`;

      const rp = running.get(name);
      const isActuallyRunning = rp && rp.proc.exitCode === null;

      return [
        `Name: ${record.name}`,
        `Status: ${isActuallyRunning ? "running" : record.status}`,
        `PID: ${isActuallyRunning ? rp.proc.pid : "N/A"}`,
        `Command: ${record.command}`,
        `CWD: ${record.cwd ?? "default"}`,
        `Restart Policy: ${record.restart_policy}`,
        `Restart Count: ${record.restart_count}/${record.max_restarts}`,
        `Last Exit Code: ${record.last_exit_code ?? "N/A"}`,
      ].join("\n");
    },

    logs(name, lines = 50) {
      const rp = running.get(name);
      if (!rp) return `Process "${name}" not found or not running`;

      const logLines = rp.logs.getLines(lines);
      if (logLines.length === 0) return `No logs for "${name}"`;

      return logLines.join("\n");
    },

    list() {
      const records = listProcessRecords();
      if (records.length === 0) return "No managed processes.";

      return records
        .map((r) => {
          const rp = running.get(r.name);
          const actualStatus = rp && rp.proc.exitCode === null ? "running" : r.status;
          const pidStr = rp && rp.proc.exitCode === null ? ` (PID ${rp.proc.pid})` : "";
          return `${r.name}: ${actualStatus}${pidStr} [${r.restart_policy}]`;
        })
        .join("\n");
    },
  };
}
