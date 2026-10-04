import { z } from "zod";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { homedir } from "node:os";

const TelegramConfigSchema = z.object({
  botToken: z.string().min(1, "Telegram bot token required"),
  allowedUsers: z.array(z.number()).min(1, "At least one allowed user ID required"),
  allowedUsernames: z.array(z.string()).default([]),
});

const AnthropicConfigSchema = z.object({
  apiKey: z.string().optional(), // Optional — falls back to OAuth token
  model: z.string().default("claude-opus-4-6"),
  maxTokens: z.number().default(16384),
  thinking: z
    .object({
      type: z.literal("enabled"),
      budgetTokens: z.number().default(16000),
    })
    .optional(),
});

const OpenAIConfigSchema = z.object({
  apiKey: z.string().optional(),
  embeddingModel: z.string().default("text-embedding-3-small"),
}).optional();

const WorkspaceConfigSchema = z.object({
  dir: z.string().default(homedir()),
  memoryDir: z.string().default("~/.smolclaw/memory"),
});

const DaemonConfigSchema = z.object({
  logLevel: z.enum(["trace", "debug", "info", "warn", "error", "fatal"]).default("info"),
  dataDir: z.string().default("~/.smolclaw"),
});

const ConfigSchema = z.object({
  telegram: TelegramConfigSchema,
  anthropic: AnthropicConfigSchema,
  openai: OpenAIConfigSchema,  // Optional — without it, memory uses keyword search only
  workspace: WorkspaceConfigSchema.default({
    dir: homedir(),
    memoryDir: "~/.smolclaw/memory",
  }),
  daemon: DaemonConfigSchema.default({
    logLevel: "info" as const,
    dataDir: "~/.smolclaw",
  }),
});

export type Config = z.infer<typeof ConfigSchema>;

function expandHome(p: string): string {
  if (p.startsWith("~/")) {
    return resolve(homedir(), p.slice(2));
  }
  return resolve(p);
}

export function loadConfig(configPath?: string): Config {
  const path = configPath ?? expandHome("~/.smolclaw/config.json");

  if (!existsSync(path)) {
    throw new Error(
      `Config file not found at ${path}. Create it from config.example.json`
    );
  }

  const raw = readFileSync(path, "utf-8");
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Config file at ${path} is not valid JSON`);
  }

  const result = ConfigSchema.safeParse(parsed);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  - ${i.path.join(".")}: ${i.message}`)
      .join("\n");
    throw new Error(`Invalid configuration:\n${issues}`);
  }

  const config = result.data;

  // Expand ~ in paths
  config.workspace.dir = expandHome(config.workspace.dir);
  config.workspace.memoryDir = expandHome(config.workspace.memoryDir);
  config.daemon.dataDir = expandHome(config.daemon.dataDir);

  return config;
}
