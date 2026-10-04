import { getLogger } from "../logger.ts";

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

/**
 * Search the web using DuckDuckGo's HTML interface.
 * No API key needed.
 */
export async function webSearch(query: string, maxResults = 5): Promise<string> {
  const log = getLogger();

  try {
    const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
    const res = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      },
    });

    if (!res.ok) {
      return `Search failed (${res.status})`;
    }

    const html = await res.text();
    const results = parseSearchResults(html, maxResults);

    if (results.length === 0) {
      return "No results found.";
    }

    return results
      .map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`)
      .join("\n\n");
  } catch (err) {
    log.error({ err }, "Web search failed");
    return `Search error: ${err instanceof Error ? err.message : String(err)}`;
  }
}

function parseSearchResults(html: string, max: number): SearchResult[] {
  const results: SearchResult[] = [];

  // DuckDuckGo HTML results are in <a class="result__a"> tags
  const resultPattern = /<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi;
  const snippetPattern = /<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)<\/a>/gi;

  const links: { url: string; title: string }[] = [];
  let match;

  while ((match = resultPattern.exec(html)) !== null && links.length < max) {
    const rawUrl = match[1]!;
    const title = stripHtml(match[2]!).trim();

    // DuckDuckGo wraps URLs in a redirect — extract the actual URL
    let url = rawUrl;
    const uddgMatch = rawUrl.match(/[?&]uddg=([^&]+)/);
    if (uddgMatch) {
      url = decodeURIComponent(uddgMatch[1]!);
    }

    if (title && url.startsWith("http")) {
      links.push({ url, title });
    }
  }

  const snippets: string[] = [];
  while ((match = snippetPattern.exec(html)) !== null) {
    snippets.push(stripHtml(match[1]!).trim());
  }

  for (let i = 0; i < links.length && i < max; i++) {
    results.push({
      title: links[i]!.title,
      url: links[i]!.url,
      snippet: snippets[i] ?? "",
    });
  }

  return results;
}

function stripHtml(html: string): string {
  return html
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
