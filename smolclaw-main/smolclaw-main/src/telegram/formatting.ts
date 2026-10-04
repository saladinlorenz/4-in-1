const TG_MAX_LENGTH = 4096;

/**
 * Split a long message into Telegram-safe chunks (max 4096 chars).
 * Tries to split at newlines, then sentence boundaries, then word boundaries.
 */
export function splitMessage(text: string, maxLength = TG_MAX_LENGTH): string[] {
  if (text.length <= maxLength) return [text];

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= maxLength) {
      chunks.push(remaining);
      break;
    }

    let splitIndex = remaining.lastIndexOf("\n", maxLength);
    if (splitIndex <= 0 || splitIndex < maxLength * 0.3) {
      splitIndex = remaining.lastIndexOf(". ", maxLength);
      if (splitIndex > 0) splitIndex += 1; // Keep the period
    }
    if (splitIndex <= 0 || splitIndex < maxLength * 0.3) {
      splitIndex = remaining.lastIndexOf(" ", maxLength);
    }
    if (splitIndex <= 0) {
      splitIndex = maxLength;
    }

    chunks.push(remaining.slice(0, splitIndex).trimEnd());
    remaining = remaining.slice(splitIndex).trimStart();
  }

  return chunks;
}

/**
 * Escape special characters for Telegram MarkdownV2.
 */
export function escapeMarkdownV2(text: string): string {
  return text.replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, "\\$1");
}

/**
 * Format text for Telegram HTML mode (safer than MarkdownV2 for mixed content).
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Convert basic markdown (from Claude responses) to Telegram HTML.
 * Handles: bold, italic, code, code blocks, links.
 */
export function markdownToTelegramHtml(text: string): string {
  let result = escapeHtml(text);

  // Code blocks first (```lang\n...\n```)
  result = result.replace(/```(?:\w+)?\n([\s\S]*?)```/g, "<pre>$1</pre>");
  // Inline code
  result = result.replace(/`([^`]+)`/g, "<code>$1</code>");
  // Bold **text**
  result = result.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  // Italic *text*
  result = result.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, "<i>$1</i>");

  return result;
}
