import type { ViewerReportFragment } from "@memoryking/contracts";

export type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph" | "code" | "item"; text: string }
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "figure"; caption: string; reference: string };

export function block(raw: unknown): Block {
  if (!raw || typeof raw !== "object") throw new Error("invalid_report");
  const b = raw as Block;
  if (["paragraph", "code", "item", "heading"].includes(b.kind)) {
    if (!("text" in b) || typeof b.text !== "string" || b.text.length > 2097152)
      throw new Error("invalid_report");
    if (
      b.kind === "heading" &&
      (!Number.isInteger(b.level) || b.level < 1 || b.level > 6)
    )
      throw new Error("invalid_report");
  } else if (b.kind === "figure") {
    if (
      typeof b.caption !== "string" ||
      !/^[A-Za-z0-9_-]{1,96}\.(png|jpg|jpeg)$/.test(b.reference)
    )
      throw new Error("invalid_report");
  } else if (b.kind === "table") {
    if (
      !Array.isArray(b.headers) ||
      b.headers.length < 1 ||
      b.headers.length > 16 ||
      !b.headers.every((x) => typeof x === "string") ||
      !Array.isArray(b.rows) ||
      b.rows.length > 65536 ||
      !b.rows.every(
        (row) =>
          Array.isArray(row) &&
          row.length === b.headers.length &&
          row.every((x) => typeof x === "string"),
      )
    )
      throw new Error("invalid_report");
  } else throw new Error("invalid_report");
  return b;
}

export class ReportBuffer {
  readonly blocks: Block[] = [];
  private parts: string[] = [];
  private count = 0;
  private bytes = 0;
  page = 0;
  cursor: string | null = null;
  constructor(readonly pages: number) {
    if (!Number.isSafeInteger(pages) || pages < 1 || pages > 16384)
      throw new Error("invalid_report");
  }
  append(
    page: number,
    pages: number,
    fragment: ViewerReportFragment,
    next: string | null,
  ): void {
    if (
      page !== this.page ||
      pages !== this.pages ||
      fragment.version !== 1 ||
      fragment.encoding !== "canonical-block-json" ||
      fragment.block !== this.blocks.length ||
      fragment.part !== this.parts.length ||
      !Number.isSafeInteger(fragment.parts) ||
      fragment.parts < 1 ||
      fragment.parts > 3073 ||
      (this.parts.length > 0 && fragment.parts !== this.count) ||
      typeof fragment.content !== "string" ||
      [...fragment.content].length > 4096 ||
      (next === null) !== (page + 1 === pages)
    )
      throw new Error("invalid_report");
    this.bytes += new TextEncoder().encode(fragment.content).length;
    if (this.bytes > 16777216 || this.blocks.length >= 8192)
      throw new Error("report_limit");
    this.count = fragment.parts;
    this.parts.push(fragment.content);
    if (this.parts.length === this.count) {
      this.blocks.push(block(JSON.parse(this.parts.join(""))));
      this.parts = [];
      this.count = 0;
    }
    this.page++;
    this.cursor = next;
    if (this.page === this.pages && this.parts.length)
      throw new Error("invalid_report");
  }
  get complete(): boolean {
    return this.page === this.pages && this.parts.length === 0;
  }
  clear(): void {
    this.blocks.length = 0;
    this.parts.length = 0;
    this.bytes = 0;
    this.count = 0;
    this.page = 0;
    this.cursor = null;
  }
}

export function safeLink(value: string): string | null {
  if (/[\u0000-\u0020\u007f]/.test(value) || value.length > 4096) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
