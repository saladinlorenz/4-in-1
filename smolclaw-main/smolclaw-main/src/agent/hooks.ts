import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";

/**
 * Pre-tool execution hook: audit logging and safety checks.
 */
export function preToolHook(sessionId: string, toolName: string, input: Record<string, unknown>): boolean {
  const log = getLogger();

  // Safety check: block dangerous bash commands
  if (toolName === "bash" || toolName === "Bash") {
    const command = String(input.command ?? "");
    const dangerous = [
      /rm\s+-rf\s+\//,
      /mkfs\./,
      /dd\s+if=/,
      /:\(\)\s*\{\s*:\|:&\s*\}/,  // fork bomb
      /shutdown/,
      /reboot/,
    ];

    for (const pattern of dangerous) {
      if (pattern.test(command)) {
        log.warn({ sessionId, toolName, command }, "Blocked dangerous command");
        return false;
      }
    }
  }

  return true;
}

/**
 * Post-tool execution hook: logs tool execution results.
 */
export function postToolHook(
  sessionId: string,
  toolName: string,
  input: Record<string, unknown>,
  output: string,
  durationMs: number
): void {
  const log = getLogger();

  log.info(
    {
      sessionId,
      tool: toolName,
      durationMs,
      outputLength: output.length,
    },
    "Tool execution completed"
  );

  try {
    const db = getDatabase();
    db.prepare(
      "INSERT INTO audit_log (session_id, action, detail) VALUES (?, ?, ?)"
    ).run(
      sessionId,
      "tool_complete",
      JSON.stringify({
        tool: toolName,
        duration_ms: durationMs,
        output_length: output.length,
      })
    );
  } catch {
    // Non-critical
  }
}
