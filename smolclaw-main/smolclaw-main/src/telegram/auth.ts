import type { Context, NextFunction } from "grammy";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";

export function createAuthMiddleware(config: Config) {
  const allowedIds = new Set(config.telegram.allowedUsers);
  const allowedUsernames = new Set(config.telegram.allowedUsernames.map((u) => u.toLowerCase()));
  const log = getLogger();

  return async (ctx: Context, next: NextFunction) => {
    const userId = ctx.from?.id;
    const username = ctx.from?.username?.toLowerCase();

    if (!userId) {
      log.warn("Message with no user ID, ignoring");
      return;
    }

    if (allowedIds.has(userId) || (username && allowedUsernames.has(username))) {
      return next();
    }

    log.warn({ userId, username }, "Unauthorized access attempt");
    // Silently ignore — don't reveal the bot exists to strangers
  };
}
