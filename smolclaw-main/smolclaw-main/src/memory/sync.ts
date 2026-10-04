import { watch, readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve, extname } from "node:path";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";
import { addMemory } from "./store.ts";

let watcher: ReturnType<typeof watch> | undefined;

/**
 * Watch the memory directory for .md file changes.
 * Auto-indexes new/changed files as memory entries.
 */
export function startMemorySync(config: Config) {
  const memDir = config.workspace.memoryDir;
  const log = getLogger();

  if (!existsSync(memDir)) {
    log.warn({ memDir }, "Memory directory does not exist, skipping sync");
    return;
  }

  // Index existing files on startup
  indexDirectory(memDir);

  // Watch for changes
  try {
    watcher = watch(memDir, { recursive: false }, (event, filename) => {
      if (!filename || !filename.endsWith(".md")) return;

      const filePath = resolve(memDir, filename);
      log.info({ filePath, event }, "Memory file changed");

      try {
        if (existsSync(filePath)) {
          const content = readFileSync(filePath, "utf-8");
          if (content.trim()) {
            addMemory(content, "file", filePath, [`file:${filename}`]);
          }
        }
      } catch (err) {
        log.error({ err, filePath }, "Failed to index memory file");
      }
    });
    log.info({ memDir }, "Memory file sync watching");
  } catch (err) {
    log.error({ err }, "Failed to start memory file watcher");
  }
}

function indexDirectory(dir: string) {
  const log = getLogger();
  try {
    const files = readdirSync(dir);
    for (const file of files) {
      if (extname(file) !== ".md") continue;
      const filePath = resolve(dir, file);
      try {
        const content = readFileSync(filePath, "utf-8");
        if (content.trim()) {
          addMemory(content, "file", filePath, [`file:${file}`]);
        }
      } catch (err) {
        log.warn({ err, filePath }, "Failed to index existing memory file");
      }
    }
    log.info({ dir, fileCount: files.filter((f) => f.endsWith(".md")).length }, "Indexed existing memory files");
  } catch (err) {
    log.warn({ err, dir }, "Failed to read memory directory");
  }
}

export function stopMemorySync() {
  if (watcher) {
    watcher.close();
    watcher = undefined;
  }
}
