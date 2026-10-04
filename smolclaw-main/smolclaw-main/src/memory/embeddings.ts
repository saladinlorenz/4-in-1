import OpenAI from "openai";
import type { Config } from "../config.ts";
import { getLogger } from "../logger.ts";
import { withRetry } from "../utils/retry.ts";

let client: OpenAI | undefined;
let model: string;

export function initEmbeddings(config: Config) {
  if (!config.openai?.apiKey) return; // No OpenAI key — embeddings disabled, FTS-only
  client = new OpenAI({ apiKey: config.openai.apiKey });
  model = config.openai.embeddingModel;
}

export async function getEmbedding(text: string): Promise<Float32Array | null> {
  if (!client) {
    getLogger().warn("OpenAI client not initialized, embeddings unavailable");
    return null;
  }

  try {
    const response = await withRetry(
      () =>
        client!.embeddings.create({
          model,
          input: text.slice(0, 8000), // text-embedding-3-small max input
        }),
      { maxRetries: 2, baseDelayMs: 1000 }
    );

    const embedding = response.data[0]?.embedding;
    if (!embedding) return null;

    return new Float32Array(embedding);
  } catch (err) {
    getLogger().error({ err }, "Embedding generation failed");
    return null;
  }
}

export async function getEmbeddings(texts: string[]): Promise<(Float32Array | null)[]> {
  if (!client) return texts.map(() => null);

  try {
    const response = await withRetry(
      () =>
        client!.embeddings.create({
          model,
          input: texts.map((t) => t.slice(0, 8000)),
        }),
      { maxRetries: 2, baseDelayMs: 1000 }
    );

    return response.data.map((d) => new Float32Array(d.embedding));
  } catch (err) {
    getLogger().error({ err }, "Batch embedding generation failed");
    return texts.map(() => null);
  }
}
