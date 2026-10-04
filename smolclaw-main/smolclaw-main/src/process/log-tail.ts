/**
 * Ring buffer for process logs. Keeps last N lines in memory.
 */
export class LogTail {
  private buffer: string[];
  private maxLines: number;
  private writeIndex = 0;
  private count = 0;

  constructor(maxLines = 1000) {
    this.maxLines = maxLines;
    this.buffer = new Array(maxLines);
  }

  push(line: string) {
    this.buffer[this.writeIndex] = line;
    this.writeIndex = (this.writeIndex + 1) % this.maxLines;
    if (this.count < this.maxLines) this.count++;
  }

  pushChunk(chunk: string) {
    const lines = chunk.split("\n");
    for (const line of lines) {
      if (line.trim()) this.push(line);
    }
  }

  getLines(n?: number): string[] {
    const requested = Math.min(n ?? this.count, this.count);
    const result: string[] = [];

    let startIndex: number;
    if (this.count < this.maxLines) {
      startIndex = Math.max(0, this.count - requested);
    } else {
      startIndex = (this.writeIndex - requested + this.maxLines) % this.maxLines;
    }

    for (let i = 0; i < requested; i++) {
      const idx = (startIndex + i) % this.maxLines;
      const line = this.buffer[idx];
      if (line !== undefined) result.push(line);
    }

    return result;
  }

  clear() {
    this.buffer = new Array(this.maxLines);
    this.writeIndex = 0;
    this.count = 0;
  }
}
