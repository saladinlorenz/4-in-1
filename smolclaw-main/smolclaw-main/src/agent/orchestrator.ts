import Anthropic from "@anthropic-ai/sdk";
import type { Context } from "grammy";
import type { Config } from "../config.ts";
import type { TelegramBot } from "../telegram/bot.ts";
import { buildSystemPrompt } from "./system-prompt.ts";
import { getAllCustomTools } from "./tools.ts";
import { getDatabase } from "../db/connection.ts";
import { getLogger } from "../logger.ts";
import { markdownToTelegramHtml } from "../telegram/formatting.ts";
import { getAccessToken } from "../auth.ts";

interface SessionHistory {
  role: "user" | "assistant";
  content: string;
}

type ToolHandler = (input: Record<string, unknown>) => Promise<string>;

export interface Orchestrator {
  handleMessage(ctx: Context): Promise<void>;
  runPrompt(sessionId: string, prompt: string, chatId?: number | string): Promise<string>;
  registerTool(name: string, handler: ToolHandler): void;
}

/** Format a one-liner describing what a tool call did */
function toolHeader(name: string, input: Record<string, unknown>): string {
  switch (name) {
    case "bash":
      return `$ ${(input.command as string ?? "").slice(0, 120)}`;
    case "read_file":
      return `read ${input.path as string ?? ""}`;
    case "write_file":
      return `wrote ${input.path as string ?? ""}`;
    case "list_files":
      return `ls ${input.pattern as string ?? ""}`;
    case "web_search":
      return `search: "${input.query as string ?? ""}"`;
    case "web_fetch":
      return `fetch ${(input.url as string ?? "").slice(0, 100)}`;
    case "memory_write":
      return `saved to memory`;
    case "memory_search":
      return `searched memory: "${input.query as string ?? ""}"`;
    case "process_manage":
      return `process ${input.action as string ?? ""} ${input.name as string ?? ""}`.trim();
    case "cron_manage":
      return `cron ${input.action as string ?? ""} ${input.name as string ?? ""}`.trim();
    case "loop_manage":
      return `loop ${input.action as string ?? ""} ${input.name as string ?? ""}`.trim();
    default:
      return name;
  }
}

function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** HTML format with expandable blockquote for output */
function formatToolFeedback(name: string, input: Record<string, unknown>, output: string): string {
  const header = escHtml(toolHeader(name, input));
  const trimmedOutput = output.length > 2000 ? output.slice(0, 2000) + "\n..." : output;
  const escapedOutput = escHtml(trimmedOutput.trim());

  if (!escapedOutput) {
    return `<b>${escHtml(name)}</b>  <code>${header}</code>`;
  }

  return `<b>${escHtml(name)}</b>  <code>${header}</code>\n<blockquote expandable>${escapedOutput}</blockquote>`;
}

/** Build a single combined HTML message: expandable tool blocks on top, response below */
function buildCombinedMessage(result: { text: string; toolCalls: { name: string; input: Record<string, unknown>; output: string }[] }): string {
  const parts: string[] = [];

  // Tool call blocks — each as a compact expandable section
  if (result.toolCalls.length > 0) {
    for (const tc of result.toolCalls) {
      const header = escHtml(toolHeader(tc.name, tc.input));
      const output = tc.output.trim();
      if (output) {
        const trimmed = output.length > 1500 ? output.slice(0, 1500) + "\n..." : output;
        parts.push(`<code>${header}</code>\n<blockquote expandable>${escHtml(trimmed)}</blockquote>`);
      } else {
        parts.push(`<code>${header}</code>`);
      }
    }
  }

  // The actual response text
  const responseText = cleanLeakedMarkup(result.text).trim();
  if (responseText) {
    parts.push(markdownToTelegramHtml(responseText));
  }

  return parts.join("\n\n");
}

/**
 * Strip leaked XML-like tool markup from responses.
 * Claude sometimes outputs <bash>cmd</bash> or <search>...</search> as text
 * when it hallucinates tool calls. Clean these up for Telegram display.
 */
function cleanLeakedMarkup(text: string): string {
  // Replace <tag>content</tag> patterns with just the content in a code block
  return text.replace(
    /<(bash|search|tool_call|function_call|command|code)>([\s\S]*?)<\/\1>/gi,
    (_, tag, content) => {
      const trimmed = content.trim();
      if (!trimmed) return "";
      return `\`\`\`\n${trimmed}\n\`\`\``;
    }
  );
}

export function createOrchestrator(config: Config, tgBot: TelegramBot): Orchestrator {
  const log = getLogger();
  const useOAuth = !config.anthropic.apiKey;
  const toolHandlers: Record<string, ToolHandler> = {};

  // Build Anthropic client — either API key or OAuth token
  async function getClient(): Promise<Anthropic> {
    if (!useOAuth) {
      return new Anthropic({ apiKey: config.anthropic.apiKey });
    }
    const token = await getAccessToken();
    return new Anthropic({ authToken: token });
  }

  function getSession(sessionId: string): SessionHistory[] {
    const db = getDatabase();
    const row = db
      .query<{ history: string }, [string]>("SELECT history FROM sessions WHERE id = ?")
      .get(sessionId);
    if (row) {
      try {
        return JSON.parse(row.history);
      } catch {
        return [];
      }
    }
    return [];
  }

  function saveSession(sessionId: string, history: SessionHistory[]): void {
    const db = getDatabase();
    const trimmed = history.slice(-100);
    const historyJson = JSON.stringify(trimmed);
    db.prepare(
      `INSERT INTO sessions (id, history, updated_at) VALUES (?, ?, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET history = excluded.history, updated_at = excluded.updated_at`
    ).run(sessionId, historyJson);
  }

  async function getMemoryContext(query: string): Promise<string | undefined> {
    try {
      const { injectMemory } = await import("./memory-injector.ts");
      return await injectMemory(query, config);
    } catch {
      return undefined;
    }
  }

  function writeAudit(sessionId: string, action: string, detail: string, tokensIn?: number, tokensOut?: number) {
    try {
      const db = getDatabase();
      db.prepare(
        "INSERT INTO audit_log (session_id, action, detail, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)"
      ).run(sessionId, action, detail, tokensIn ?? null, tokensOut ?? null);
    } catch (err) {
      log.warn({ err }, "Failed to write audit log");
    }
  }

  interface AgentResult {
    text: string;
    toolCalls: { name: string; input: Record<string, unknown>; output: string }[];
  }

  async function handleAgentLoop(
    sessionId: string,
    userMessage: string,
    chatId?: number | string
  ): Promise<AgentResult> {
    const history = getSession(sessionId);
    const memoryContext = await getMemoryContext(userMessage);
    const systemPrompt = buildSystemPrompt(config, memoryContext);

    const messages: Anthropic.MessageParam[] = [];
    for (const entry of history) {
      messages.push({ role: entry.role, content: entry.content });
    }
    messages.push({ role: "user", content: userMessage });

    const customTools = getAllCustomTools();
    let fullResponse = "";
    const toolCalls: { name: string; input: Record<string, unknown>; output: string }[] = [];
    let continueLoop = true;
    let iterations = 0;
    const maxIterations = 20;

    while (continueLoop && iterations < maxIterations) {
      iterations++;

      const requestParams: Anthropic.MessageCreateParams = {
        model: config.anthropic.model,
        max_tokens: config.anthropic.maxTokens,
        system: systemPrompt,
        messages,
        tools: customTools,
      };

      if (config.anthropic.thinking) {
        (requestParams as any).thinking = config.anthropic.thinking;
      }

      log.info({ sessionId, messageCount: messages.length, iteration: iterations }, "Calling Anthropic API");

      const client = await getClient();
      const response = await client.messages.create(requestParams);

      log.info(
        {
          sessionId,
          stopReason: response.stop_reason,
          inputTokens: response.usage.input_tokens,
          outputTokens: response.usage.output_tokens,
        },
        "Anthropic API response"
      );

      writeAudit(
        sessionId,
        "response",
        JSON.stringify({ stop_reason: response.stop_reason, iteration: iterations }),
        response.usage.input_tokens,
        response.usage.output_tokens
      );

      const assistantContent: Anthropic.ContentBlockParam[] = [];
      const toolResults: Anthropic.ToolResultBlockParam[] = [];

      for (const block of response.content) {
        if (block.type === "text") {
          fullResponse += block.text;
          assistantContent.push({ type: "text", text: block.text });
        } else if (block.type === "thinking") {
          log.debug({ thinking: (block as any).thinking?.slice(0, 200) }, "Agent thinking");
        } else if (block.type === "tool_use") {
          assistantContent.push({
            type: "tool_use",
            id: block.id,
            name: block.name,
            input: block.input,
          });
          const toolName = block.name;
          const toolInput = block.input as Record<string, unknown>;

          log.info({ tool: toolName }, "Tool call");

          let toolOutput: string;
          if (toolHandlers[toolName]) {
            try {
              toolOutput = await toolHandlers[toolName](toolInput);
            } catch (err) {
              toolOutput = `Error: ${err instanceof Error ? err.message : String(err)}`;
              log.error({ err, tool: toolName }, "Tool handler error");
            }
          } else {
            toolOutput = `Error: Unknown tool "${toolName}". Available custom tools: ${Object.keys(toolHandlers).join(", ")}`;
          }

          toolCalls.push({ name: toolName, input: toolInput, output: toolOutput });

          toolResults.push({
            type: "tool_result",
            tool_use_id: block.id,
            content: toolOutput,
          });

          writeAudit(
            sessionId,
            "tool_call",
            JSON.stringify({ tool: toolName, output: toolOutput.slice(0, 1000) })
          );
        }
      }

      if (toolResults.length > 0) {
        messages.push({ role: "assistant", content: assistantContent });
        messages.push({ role: "user", content: toolResults });
        continueLoop = response.stop_reason === "tool_use";
      } else {
        continueLoop = false;
      }
    }

    if (iterations >= maxIterations) {
      fullResponse += "\n\n(Reached maximum tool call iterations)";
      log.warn({ sessionId, iterations }, "Hit max agent loop iterations");
    }

    history.push({ role: "user", content: userMessage });
    history.push({ role: "assistant", content: fullResponse });
    saveSession(sessionId, history);

    return { text: fullResponse, toolCalls };
  }

  const orchestrator: Orchestrator = {
    registerTool(name: string, handler: ToolHandler) {
      toolHandlers[name] = handler;
      log.info({ tool: name }, "Registered custom tool handler");
    },

    async handleMessage(ctx: Context) {
      const chatId = ctx.chat?.id;
      const text = ctx.message?.text;
      if (!chatId || !text) return;

      const sessionId = String(chatId);

      // Handle slash commands
      if (text.startsWith("/")) {
        const { handleCommand } = await import("../telegram/commands.ts");
        const handled = await handleCommand(ctx, config, tgBot);
        if (handled) return;
      }

      await ctx.replyWithChatAction("typing");

      try {
        const result = await handleAgentLoop(sessionId, text, chatId);
        const combined = buildCombinedMessage(result);

        if (combined.trim()) {
          try {
            await tgBot.sendChunkedMessage(chatId, combined, "HTML");
          } catch {
            // HTML failed — fall back to plain text
            const plain = cleanLeakedMarkup(result.text);
            await tgBot.sendChunkedMessage(chatId, plain);
          }
        } else {
          await tgBot.sendMessage(chatId, "(No text response — task completed silently)");
        }
      } catch (err) {
        log.error({ err, chatId }, "Agent loop error");
        const errMsg = err instanceof Error ? err.message : String(err);

        if (errMsg.includes("rate_limit") || errMsg.includes("429")) {
          await tgBot.sendMessage(chatId, "Rate limited. Retrying in 3s...");
          await Bun.sleep(3000);
          try {
            const result = await handleAgentLoop(sessionId, text, chatId);
            const combined = buildCombinedMessage(result);
            if (combined.trim()) {
              await tgBot.sendChunkedMessage(chatId, combined, "HTML");
            }
          } catch (retryErr) {
            await tgBot.sendMessage(
              chatId,
              `Still failing: ${retryErr instanceof Error ? retryErr.message : String(retryErr)}`
            );
          }
        } else {
          await tgBot.sendMessage(chatId, `Error: ${errMsg.slice(0, 500)}`);
        }
      }
    },

    async runPrompt(sessionId, prompt, chatId) {
      const result = await handleAgentLoop(sessionId, prompt, chatId);
      return result.text;
    },
  };

  return orchestrator;
}
