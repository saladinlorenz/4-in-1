import { describe, test, expect, mock } from "bun:test";
import { setLogger } from "../../src/logger.ts";
import { askUser, clearPendingQuestions } from "../../src/agent/ask-user-bridge.ts";
import pino from "pino";

setLogger(pino({ level: "silent" }));

describe("ask-user-bridge", () => {
  test("askUser sends message with inline keyboard", async () => {
    const sendMessage = mock(async (_chatId: any, _text: any, _opts: any) => ({ message_id: 1 }));
    const mockBot: any = {
      api: { sendMessage },
    };

    const promise = askUser(mockBot, 123, "Which option?", ["Option A", "Option B"]);

    await Bun.sleep(10);

    expect(sendMessage).toHaveBeenCalledTimes(1);
    const callArgs = sendMessage.mock.calls[0] as any[];
    expect(callArgs[0]).toBe(123);
    expect(callArgs[1]).toBe("Which option?");
    expect(callArgs[2]).toHaveProperty("reply_markup");

    const keyboard = (callArgs[2] as any).reply_markup;
    expect(keyboard.inline_keyboard).toHaveLength(2);
    expect(keyboard.inline_keyboard[0][0].text).toBe("Option A");
    expect(keyboard.inline_keyboard[1][0].text).toBe("Option B");

    clearPendingQuestions();
  });

  test("clearPendingQuestions resolves all pending", async () => {
    const mockBot: any = {
      api: { sendMessage: mock(async () => ({ message_id: 1 })) },
    };

    const promise = askUser(mockBot, 123, "Test?", ["A", "B"]);
    await Bun.sleep(10);

    clearPendingQuestions();

    const result = await promise;
    expect(result).toContain("Session ended");
  });
});
