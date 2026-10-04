import type { Orchestrator } from "../agent/orchestrator.ts";
import type { TelegramBot } from "../telegram/bot.ts";
import type { Config } from "../config.ts";
import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { markdownToTelegramHtml } from "../telegram/formatting.ts";

interface JobPayload {
  prompt?: string;
  command?: string;
  session_id?: string;
}

export async function executeJob(
  jobId: string,
  actionType: string,
  payloadJson: string,
  config: Config,
  orchestrator: Orchestrator,
  bot: TelegramBot
): Promise<{ success: boolean; output: string; duration_ms: number }> {
  const log = getLogger();
  const start = Date.now();

  let payload: JobPayload;
  try {
    payload = JSON.parse(payloadJson);
  } catch {
    return { success: false, output: "Invalid job payload JSON", duration_ms: 0 };
  }

  const ownerChatId = config.telegram.allowedUsers[0];
  if (!ownerChatId) {
    return { success: false, output: "No owner chat ID configured", duration_ms: 0 };
  }

  try {
    switch (actionType) {
      case "agent": {
        const prompt = payload.prompt ?? "No prompt provided";
        const sessionId = payload.session_id ?? `cron:${jobId}`;
        const result = await orchestrator.runPrompt(sessionId, prompt);

        const html = markdownToTelegramHtml(result);
        await bot.sendChunkedMessage(ownerChatId, `<b>[Cron]</b> ${html}`, "HTML");

        return { success: true, output: result.slice(0, 1000), duration_ms: Date.now() - start };
      }

      case "shell": {
        const command = payload.command ?? "echo 'No command'";
        const proc = Bun.spawn(["sh", "-c", command], {
          cwd: config.workspace.dir,
          stdout: "pipe",
          stderr: "pipe",
        });

        const stdout = await new Response(proc.stdout).text();
        const stderr = await new Response(proc.stderr).text();
        const exitCode = await proc.exited;

        const output = stdout + (stderr ? `\n[stderr] ${stderr}` : "");
        const summary = output.slice(0, 2000);

        await bot.sendMessage(
          ownerChatId,
          `<b>[Cron Shell]</b> Exit ${exitCode}\n<pre>${summary}</pre>`,
          "HTML"
        );

        return {
          success: exitCode === 0,
          output: summary,
          duration_ms: Date.now() - start,
        };
      }

      case "check": {
        const prompt = payload.prompt ?? "Check if anything needs attention";
        const sessionId = payload.session_id ?? `cron:${jobId}`;
        const result = await orchestrator.runPrompt(sessionId, prompt);

        // Only notify if the agent found something noteworthy
        const lowerResult = result.toLowerCase();
        const isNotable =
          !lowerResult.includes("nothing to report") &&
          !lowerResult.includes("all clear") &&
          !lowerResult.includes("no issues") &&
          !lowerResult.includes("everything looks good") &&
          result.trim().length > 20;

        if (isNotable) {
          const html = markdownToTelegramHtml(result);
          await bot.sendChunkedMessage(ownerChatId, `<b>[Alert]</b> ${html}`, "HTML");
        }

        return {
          success: true,
          output: isNotable ? result.slice(0, 1000) : "(silent — nothing to report)",
          duration_ms: Date.now() - start,
        };
      }

      default:
        return {
          success: false,
          output: `Unknown action type: ${actionType}`,
          duration_ms: Date.now() - start,
        };
    }
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    log.error({ err, jobId, actionType }, "Cron job execution failed");

    try {
      await bot.sendMessage(ownerChatId, `<b>[Cron Error]</b> Job ${jobId}: ${errMsg}`, "HTML");
    } catch {
      // Ignore notification failure
    }

    return {
      success: false,
      output: errMsg.slice(0, 1000),
      duration_ms: Date.now() - start,
    };
  }
}
