import pino from "pino";
import type { Config } from "./config.ts";

let logger: pino.Logger | undefined;

export function initLogger(config: Config): pino.Logger {
  logger = pino({
    level: config.daemon.logLevel,
    transport: {
      target: "pino/file",
      options: {
        destination: `${config.daemon.dataDir}/smolclaw.log`,
        mkdir: true,
      },
    },
  });
  return logger;
}

/**
 * Set logger directly (used for testing).
 */
export function setLogger(l: pino.Logger): void {
  logger = l;
}

export function getLogger(): pino.Logger {
  if (!logger) {
    // Fallback for early startup before config is loaded
    logger = pino({ level: "info" });
  }
  return logger;
}
