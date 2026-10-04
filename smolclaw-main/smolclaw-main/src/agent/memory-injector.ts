import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";

/**
 * Mandatory memory injection: loads DIRECTIVES.md + hybrid-searched context.
 * Called by the orchestrator BEFORE every query — the agent doesn't choose this.
 */
export async function injectMemory(query: string, config: Config): Promise<string | undefined> {
  const log = getLogger();
  const parts: string[] = [];

  // 1. Always load DIRECTIVES.md
  const directivesPath = resolve(config.workspace.memoryDir, "DIRECTIVES.md");
  if (existsSync(directivesPath)) {
    try {
      const directives = readFileSync(directivesPath, "utf-8").trim();
      if (directives) {
        parts.push(`### DIRECTIVES (always loaded)\n${directives}`);
      }
    } catch (err) {
      log.warn({ err }, "Failed to read DIRECTIVES.md");
    }
  }

  // 2. Hybrid search for query-relevant memories
  try {
    const { searchMemory } = await import("../memory/search.ts");
    const results = await searchMemory(query, 10);
    if (results.length > 0) {
      const memoryBlock = results
        .map(
          (r, i) =>
            `[${i + 1}] (${r.source}, ${r.created_at}) ${r.content.slice(0, 500)}${r.content.length > 500 ? "..." : ""}`
        )
        .join("\n\n");
      parts.push(`### Relevant Memories\n${memoryBlock}`);
    }
  } catch (err) {
    log.debug({ err }, "Memory search unavailable during injection");
  }

  if (parts.length === 0) return undefined;
  return parts.join("\n\n---\n\n");
}
