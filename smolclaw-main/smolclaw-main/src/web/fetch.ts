import { getLogger } from "../logger.ts";

/**
 * Fetch a URL and return readable text content (HTML stripped).
 */
export async function webFetch(url: string, maxLength = 20_000): Promise<string> {
  const log = getLogger();

  try {
    const res = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
      redirect: "follow",
      signal: AbortSignal.timeout(15_000),
    });

    if (!res.ok) {
      return `Fetch failed (${res.status} ${res.statusText})`;
    }

    const contentType = res.headers.get("content-type") ?? "";

    // If it's JSON, return it directly
    if (contentType.includes("application/json")) {
      const text = await res.text();
      return text.length > maxLength ? text.slice(0, maxLength) + "\n..." : text;
    }

    // If it's plain text, return directly
    if (contentType.includes("text/plain")) {
      const text = await res.text();
      return text.length > maxLength ? text.slice(0, maxLength) + "\n..." : text;
    }

    // HTML — strip tags and extract readable content
    const html = await res.text();
    const text = htmlToReadableText(html);

    return text.length > maxLength ? text.slice(0, maxLength) + "\n..." : text;
  } catch (err) {
    log.error({ err, url }, "Web fetch failed");
    return `Fetch error: ${err instanceof Error ? err.message : String(err)}`;
  }
}

function htmlToReadableText(html: string): string {
  let text = html;

  // Remove script and style blocks entirely
  text = text.replace(/<script[\s\S]*?<\/script>/gi, "");
  text = text.replace(/<style[\s\S]*?<\/style>/gi, "");
  text = text.replace(/<noscript[\s\S]*?<\/noscript>/gi, "");
  text = text.replace(/<!--[\s\S]*?-->/g, "");

  // Convert common block elements to newlines
  text = text.replace(/<\/?(p|div|br|hr|h[1-6]|li|tr|blockquote|pre|section|article|header|footer|nav|main)[^>]*>/gi, "\n");
  text = text.replace(/<\/?(ul|ol|table|thead|tbody)[^>]*>/gi, "\n");
  text = text.replace(/<td[^>]*>/gi, "\t");

  // Strip remaining HTML tags
  text = text.replace(/<[^>]+>/g, "");

  // Decode HTML entities
  text = text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)));

  // Clean up whitespace
  text = text
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter((line) => line.length > 0)
    .join("\n");

  // Collapse multiple blank lines
  text = text.replace(/\n{3,}/g, "\n\n");

  return text.trim();
}
