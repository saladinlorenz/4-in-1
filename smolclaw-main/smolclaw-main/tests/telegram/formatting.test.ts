import { describe, test, expect } from "bun:test";
import {
  splitMessage,
  escapeMarkdownV2,
  escapeHtml,
  markdownToTelegramHtml,
} from "../../src/telegram/formatting.ts";

describe("splitMessage", () => {
  test("returns single chunk for short messages", () => {
    const chunks = splitMessage("Hello world");
    expect(chunks).toEqual(["Hello world"]);
  });

  test("splits at newline boundaries", () => {
    const text = "A".repeat(4000) + "\n" + "B".repeat(100);
    const chunks = splitMessage(text);
    expect(chunks.length).toBe(2);
    expect(chunks[0]!.length).toBeLessThanOrEqual(4096);
  });

  test("handles text exactly at limit", () => {
    const text = "A".repeat(4096);
    const chunks = splitMessage(text);
    expect(chunks.length).toBe(1);
  });

  test("handles text just over limit", () => {
    const text = "A".repeat(4097);
    const chunks = splitMessage(text);
    expect(chunks.length).toBe(2);
  });

  test("custom max length", () => {
    const chunks = splitMessage("Hello World", 5);
    expect(chunks.length).toBeGreaterThan(1);
  });

  test("splits at sentence boundary when no newline", () => {
    const text = "A".repeat(3000) + ". " + "B".repeat(2000);
    const chunks = splitMessage(text);
    expect(chunks.length).toBe(2);
  });

  test("returns empty array for empty string", () => {
    const chunks = splitMessage("");
    expect(chunks).toEqual([""]);
  });
});

describe("escapeMarkdownV2", () => {
  test("escapes special characters", () => {
    expect(escapeMarkdownV2("*bold* _italic_")).toBe("\\*bold\\* \\_italic\\_");
  });

  test("escapes brackets", () => {
    expect(escapeMarkdownV2("[link](url)")).toBe("\\[link\\]\\(url\\)");
  });
});

describe("escapeHtml", () => {
  test("escapes HTML entities", () => {
    expect(escapeHtml("<b>test</b>")).toBe("&lt;b&gt;test&lt;/b&gt;");
  });

  test("escapes ampersands", () => {
    expect(escapeHtml("a & b")).toBe("a &amp; b");
  });
});

describe("markdownToTelegramHtml", () => {
  test("converts bold", () => {
    const result = markdownToTelegramHtml("**hello**");
    expect(result).toBe("<b>hello</b>");
  });

  test("converts inline code", () => {
    const result = markdownToTelegramHtml("`code`");
    expect(result).toBe("<code>code</code>");
  });

  test("converts code blocks", () => {
    const result = markdownToTelegramHtml("```js\nconst x = 1;\n```");
    expect(result).toContain("<pre>");
    expect(result).toContain("const x = 1;");
  });

  test("escapes HTML in content", () => {
    const result = markdownToTelegramHtml("<script>alert('xss')</script>");
    expect(result).not.toContain("<script>");
    expect(result).toContain("&lt;script&gt;");
  });
});
