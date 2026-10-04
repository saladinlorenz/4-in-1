import type { Bot } from "grammy";
import { InputFile } from "grammy";
import { readFileSync, existsSync } from "node:fs";
import { writeFileSync } from "node:fs";
import { resolve, basename } from "node:path";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";

/**
 * Download a file from Telegram and save it to the workspace.
 */
export async function downloadTelegramFile(
  bot: Bot,
  fileId: string,
  config: Config,
  targetDir?: string
): Promise<string> {
  const log = getLogger();
  const file = await bot.api.getFile(fileId);

  if (!file.file_path) {
    throw new Error("File path not available from Telegram");
  }

  const response = await fetch(
    `https://api.telegram.org/file/bot${config.telegram.botToken}/${file.file_path}`
  );

  if (!response.ok) {
    throw new Error(`Failed to download file: ${response.statusText}`);
  }

  const buffer = await response.arrayBuffer();
  const fileName = basename(file.file_path);
  const dir = targetDir ?? config.workspace.dir;
  const savePath = resolve(dir, fileName);

  writeFileSync(savePath, Buffer.from(buffer));
  log.info({ savePath, size: buffer.byteLength }, "Downloaded file from Telegram");

  return savePath;
}

/**
 * Upload a local file to a Telegram chat.
 */
export async function uploadFileToTelegram(
  bot: Bot,
  chatId: number | string,
  filePath: string
): Promise<void> {
  const log = getLogger();

  if (!existsSync(filePath)) {
    throw new Error(`File not found: ${filePath}`);
  }

  const fileContent = readFileSync(filePath);
  const fileName = basename(filePath);

  await bot.api.sendDocument(chatId, new InputFile(fileContent, fileName));
  log.info({ chatId, filePath, fileName }, "Uploaded file to Telegram");
}
