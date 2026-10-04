import { describe, test, expect, mock } from "bun:test";
import { createAuthMiddleware } from "../../src/telegram/auth.ts";
import type { Config } from "../../src/config.ts";

// Minimal config for testing
const config = {
  telegram: {
    botToken: "test",
    allowedUsers: [111, 222],
    allowedUsernames: ["alice", "Bob"],
  },
  anthropic: { apiKey: "test", model: "test", maxTokens: 1000 },
  openai: { apiKey: "test", embeddingModel: "test" },
  workspace: { dir: "/tmp", memoryDir: "/tmp/mem" },
  daemon: { logLevel: "info" as const, dataDir: "/tmp" },
} satisfies Config;

function makeCtx(userId?: number, username?: string) {
  return {
    from: userId ? { id: userId, username } : undefined,
  };
}

describe("auth middleware", () => {
  const middleware = createAuthMiddleware(config);

  test("allows user by ID", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx(111) as any, next);
    expect(next).toHaveBeenCalled();
  });

  test("allows user by username (case-insensitive)", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx(999, "Alice") as any, next);
    expect(next).toHaveBeenCalled();
  });

  test("allows user by username - bob", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx(999, "bob") as any, next);
    expect(next).toHaveBeenCalled();
  });

  test("blocks unauthorized user", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx(999, "stranger") as any, next);
    expect(next).not.toHaveBeenCalled();
  });

  test("blocks message with no user", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx() as any, next);
    expect(next).not.toHaveBeenCalled();
  });

  test("blocks unknown ID with no username", async () => {
    const next = mock(() => Promise.resolve());
    await middleware(makeCtx(555) as any, next);
    expect(next).not.toHaveBeenCalled();
  });
});
