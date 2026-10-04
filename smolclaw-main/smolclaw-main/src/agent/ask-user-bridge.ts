import { v4 as uuidv4 } from "uuid";
import type { Bot } from "grammy";
import { getLogger } from "../logger.ts";

interface PendingQuestion {
  resolve: (answer: string) => void;
  timeout: Timer;
}

const pending = new Map<string, PendingQuestion>();
const TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Register callback query handler on the bot for handling inline keyboard responses.
 */
export function initAskUserBridge(bot: Bot) {
  const log = getLogger();

  bot.on("callback_query:data", async (ctx) => {
    const data = ctx.callbackQuery.data;
    if (!data.startsWith("ask:")) return;

    const [, questionId, answerIndex] = data.split(":");
    if (!questionId || answerIndex === undefined) return;

    const question = pending.get(questionId);
    if (!question) {
      await ctx.answerCallbackQuery({ text: "This question has expired." });
      return;
    }

    clearTimeout(question.timeout);
    pending.delete(questionId);

    question.resolve(answerIndex);
    await ctx.answerCallbackQuery({ text: "Answer received!" });
    await ctx.editMessageReplyMarkup({ reply_markup: undefined });

    log.info({ questionId, answer: answerIndex }, "User answered inline question");
  });
}

/**
 * Send a question to a Telegram chat with inline keyboard buttons.
 * Returns a promise that resolves with the user's answer.
 */
export async function askUser(
  bot: Bot,
  chatId: number | string,
  question: string,
  options: string[]
): Promise<string> {
  const questionId = uuidv4().slice(0, 8);

  const keyboard = {
    inline_keyboard: options.map((opt, i) => [
      { text: opt, callback_data: `ask:${questionId}:${i}` },
    ]),
  };

  await bot.api.sendMessage(chatId, question, {
    reply_markup: keyboard,
  });

  return new Promise<string>((resolve) => {
    const timeout = setTimeout(() => {
      pending.delete(questionId);
      resolve("User did not respond. Proceed with your best judgment.");
    }, TIMEOUT_MS);

    pending.set(questionId, { resolve: (answerIdx) => {
      const idx = parseInt(answerIdx, 10);
      resolve(options[idx] ?? answerIdx);
    }, timeout });
  });
}

export function clearPendingQuestions() {
  for (const [id, q] of pending) {
    clearTimeout(q.timeout);
    q.resolve("Session ended before user responded.");
  }
  pending.clear();
}
