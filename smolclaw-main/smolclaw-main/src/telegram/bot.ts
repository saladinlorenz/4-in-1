import { Bot, type Context } from "grammy";
import type { Config } from "../config.ts";
import { createAuthMiddleware } from "./auth.ts";
import { getLogger } from "../logger.ts";

export interface TelegramBot {
  bot: Bot;
  start(): Promise<void>;
  stop(): void;
  sendMessage(chatId: number | string, text: string, parseMode?: "HTML" | "MarkdownV2"): Promise<void>;
  sendChunkedMessage(chatId: number | string, text: string, parseMode?: "HTML"): Promise<void>;
}

export function createBot(
  config: Config,
  onMessage: (ctx: Context) => Promise<void>
): TelegramBot {
  const log = getLogger();
  const bot = new Bot(config.telegram.botToken);

  // Auth middleware — only allowed users pass through
  bot.use(createAuthMiddleware(config));

  // Handle text messages
  bot.on("message:text", async (ctx) => {
    try {
      await onMessage(ctx);
    } catch (err) {
      log.error({ err, chatId: ctx.chat.id }, "Error handling message");
      try {
        await ctx.reply("An error occurred processing your message. Check logs for details.");
      } catch {
        // Ignore reply errors
      }
    }
  });

  // Handle documents/files
  bot.on("message:document", async (ctx) => {
    try {
      await onMessage(ctx);
    } catch (err) {
      log.error({ err, chatId: ctx.chat.id }, "Error handling document");
    }
  });

  bot.catch((err) => {
    log.error({ err: err.error }, "Unhandled bot error");
  });

  return {
    bot,

    async start() {
      log.info("Starting Telegram bot (long polling)...");
      bot.start({
        onStart: () => log.info("Telegram bot started"),
        drop_pending_updates: true,
      });
    },

    stop() {
      log.info("Stopping Telegram bot...");
      bot.stop();
    },

    async sendMessage(chatId, text, parseMode) {
      const opts = parseMode ? { parse_mode: parseMode as any } : {};
      try {
        await bot.api.sendMessage(chatId, text, opts);
      } catch (err) {
        // If HTML parse fails, retry as plain text
        if (parseMode === "HTML") {
          log.warn({ err }, "HTML parse failed, retrying as plain text");
          await bot.api.sendMessage(chatId, text);
        } else {
          throw err;
        }
      }
    },

    async sendChunkedMessage(chatId, text, parseMode) {
      const { splitMessage } = await import("./formatting.ts");
      const chunks = splitMessage(text);
      for (const chunk of chunks) {
        await this.sendMessage(chatId, chunk, parseMode);
      }
    },
  };
}
