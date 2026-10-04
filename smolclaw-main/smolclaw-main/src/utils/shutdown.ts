import { getLogger } from "../logger.ts";
import { closeDatabase } from "../db/connection.ts";

type ShutdownHandler = () => void | Promise<void>;

const handlers: ShutdownHandler[] = [];
let shuttingDown = false;

export function onShutdown(handler: ShutdownHandler) {
  handlers.push(handler);
}

export function initShutdownHandlers() {
  const log = getLogger();

  const shutdown = async (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;

    log.info({ signal }, "Shutdown signal received");

    for (const handler of handlers.reverse()) {
      try {
        await handler();
      } catch (err) {
        log.error({ err }, "Shutdown handler error");
      }
    }

    closeDatabase();
    log.info("Shutdown complete");
    process.exit(0);
  };

  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}
