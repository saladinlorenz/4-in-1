import type Anthropic from "@anthropic-ai/sdk";

/**
 * Custom tool definitions for the agent.
 * The actual execution handlers are registered in orchestrator.ts and index.ts.
 */

// ── System tools (bash, file I/O) ──────────────────────────────

export const bashTool: Anthropic.Tool = {
  name: "bash",
  description:
    "Execute a shell command and return stdout+stderr. Use for: running scripts, git, npm/bun, system commands, anything the terminal can do. Commands run in a child process with a 60-second timeout.",
  input_schema: {
    type: "object" as const,
    properties: {
      command: { type: "string", description: "The shell command to execute" },
      cwd: { type: "string", description: "Working directory (optional, defaults to workspace)" },
      timeout_ms: { type: "number", description: "Timeout in milliseconds (default 60000)" },
    },
    required: ["command"],
  },
};

export const readFileTool: Anthropic.Tool = {
  name: "read_file",
  description:
    "Read the contents of a file. Returns the file text. Use for reading configs, source code, logs, etc.",
  input_schema: {
    type: "object" as const,
    properties: {
      path: { type: "string", description: "Absolute or relative file path" },
      max_lines: { type: "number", description: "Max lines to read (default: all)" },
    },
    required: ["path"],
  },
};

export const writeFileTool: Anthropic.Tool = {
  name: "write_file",
  description:
    "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Creates parent directories automatically.",
  input_schema: {
    type: "object" as const,
    properties: {
      path: { type: "string", description: "Absolute or relative file path" },
      content: { type: "string", description: "Content to write" },
    },
    required: ["path", "content"],
  },
};

export const listFilesTool: Anthropic.Tool = {
  name: "list_files",
  description:
    "List files matching a glob pattern. Use for finding files, exploring directories.",
  input_schema: {
    type: "object" as const,
    properties: {
      pattern: { type: "string", description: "Glob pattern (e.g. 'src/**/*.ts', '*.json', '.')" },
      cwd: { type: "string", description: "Base directory (optional, defaults to workspace)" },
    },
    required: ["pattern"],
  },
};

// ── Web tools ──────────────────────────────────────────────────

export const webSearchTool: Anthropic.Tool = {
  name: "web_search",
  description:
    "Search the web and return results. Use for looking up current information, documentation, news, prices, etc.",
  input_schema: {
    type: "object" as const,
    properties: {
      query: { type: "string", description: "Search query" },
      max_results: { type: "number", description: "Max results to return (default 5)" },
    },
    required: ["query"],
  },
};

export const webFetchTool: Anthropic.Tool = {
  name: "web_fetch",
  description:
    "Fetch a URL and return the page content as readable text (HTML stripped). Use for reading articles, docs, APIs, etc.",
  input_schema: {
    type: "object" as const,
    properties: {
      url: { type: "string", description: "URL to fetch" },
      max_length: { type: "number", description: "Max characters to return (default 20000)" },
    },
    required: ["url"],
  },
};

// ── Custom domain tools ────────────────────────────────────────

export const memoryWriteTool: Anthropic.Tool = {
  name: "memory_write",
  description:
    "Save important information to persistent memory. Use this to record decisions, learned patterns, user preferences, task outcomes, and anything that should persist across conversations.",
  input_schema: {
    type: "object" as const,
    properties: {
      content: { type: "string", description: "The memory content to save" },
      tags: {
        type: "array",
        items: { type: "string" },
        description: "Tags for categorization",
      },
      source: {
        type: "string",
        enum: ["conversation", "loop", "manual"],
        description: "Source of the memory",
      },
    },
    required: ["content"],
  },
};

export const memorySearchTool: Anthropic.Tool = {
  name: "memory_search",
  description:
    "Search persistent memory for specific information. Memory is already injected into context automatically, but use this for targeted deep searches.",
  input_schema: {
    type: "object" as const,
    properties: {
      query: { type: "string", description: "Search query" },
      limit: { type: "number", description: "Max results to return (default 10)" },
    },
    required: ["query"],
  },
};

export const processManageTool: Anthropic.Tool = {
  name: "process_manage",
  description: "Manage long-running processes (bots, scripts, services).",
  input_schema: {
    type: "object" as const,
    properties: {
      action: {
        type: "string",
        enum: ["start", "stop", "restart", "status", "logs", "list"],
        description: "Action to perform",
      },
      name: { type: "string", description: "Process name" },
      command: { type: "string", description: "Shell command (for start)" },
      cwd: { type: "string", description: "Working directory (for start)" },
      restart_policy: {
        type: "string",
        enum: ["none", "on-crash", "always"],
        description: "Restart policy (for start)",
      },
      lines: {
        type: "number",
        description: "Number of log lines (for logs)",
      },
    },
    required: ["action"],
  },
};

export const cronManageTool: Anthropic.Tool = {
  name: "cron_manage",
  description: "Manage scheduled jobs.",
  input_schema: {
    type: "object" as const,
    properties: {
      action: {
        type: "string",
        enum: ["create", "delete", "list", "enable", "disable", "trigger"],
        description: "Action to perform",
      },
      name: { type: "string", description: "Job name" },
      cron_expr: { type: "string", description: "Cron expression (for create)" },
      action_type: {
        type: "string",
        enum: ["agent", "shell", "check"],
        description: "Type of action (for create)",
      },
      action_payload: {
        type: "string",
        description: "JSON payload — prompt for agent/check, command for shell",
      },
    },
    required: ["action"],
  },
};

export const loopManageTool: Anthropic.Tool = {
  name: "loop_manage",
  description:
    "Manage learning loops — cyclical processes where the agent reviews, learns, and improves over time.",
  input_schema: {
    type: "object" as const,
    properties: {
      action: {
        type: "string",
        enum: ["create", "pause", "resume", "status", "list", "history"],
        description: "Action to perform",
      },
      name: { type: "string", description: "Loop name" },
      target_process: {
        type: "string",
        description: "Name of the process to monitor (optional)",
      },
      review_cron: { type: "string", description: "Cron expression for review cycles" },
      review_prompt: {
        type: "string",
        description: "What the agent should analyze each cycle",
      },
      apply_mode: {
        type: "string",
        enum: ["suggest", "auto"],
        description: "Whether to suggest changes or auto-apply",
      },
    },
    required: ["action"],
  },
};

export function getAllCustomTools(): Anthropic.Tool[] {
  return [
    bashTool,
    readFileTool,
    writeFileTool,
    listFilesTool,
    webSearchTool,
    webFetchTool,
    memoryWriteTool,
    memorySearchTool,
    processManageTool,
    cronManageTool,
    loopManageTool,
  ];
}
