import { MemoryKingError } from "./errors.js";
import type {
  Briefing,
  HandOff,
  Progress,
  Raw,
  Remembered,
  Resumed,
  Source,
} from "./types.js";

export function rawObject(value: unknown): Raw {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Raw)
    : {};
}

function string(reply: Raw, key: string): string {
  const value = reply[key];
  if (typeof value !== "string") throw new MemoryKingError("invalid_response");
  return value;
}

function number(reply: Raw, key: string): number {
  const value = reply[key];
  if (typeof value !== "number") throw new MemoryKingError("invalid_response");
  return value;
}

export function remembered(reply: Raw): Remembered {
  return {
    observation: string(reply, "observation"),
    position: number(reply, "position"),
    held: reply.held === true,
    raw: reply,
  };
}

export function briefing(reply: Raw): Briefing {
  return {
    text: typeof reply.text === "string" ? reply.text : "",
    status: string(reply, "status"),
    receipt: string(reply, "receipt"),
    sources: Array.isArray(reply.sources) ? (reply.sources as Source[]) : [],
    warnings: Array.isArray(reply.warnings) ? (reply.warnings as string[]) : [],
    raw: reply,
  };
}

export function progress(reply: Raw): Progress {
  return {
    checkpoint: string(reply, "checkpoint"),
    revision: number(reply, "revision"),
    status: string(reply, "status"),
    receipt: string(reply, "receipt"),
    replayed: reply.replayed === true,
    raw: reply,
  };
}

export function resumed(reply: Raw): Resumed {
  return {
    text: typeof reply.text === "string" ? reply.text : "",
    status: string(reply, "status"),
    receipt: string(reply, "receipt"),
    raw: reply,
  };
}

export function handoff(reply: Raw): HandOff {
  return {
    status: string(reply, "status"),
    handle: typeof reply.handle === "string" ? reply.handle : "",
    raw: reply,
  };
}

/** A wall-clock instant: a Date, Unix ms, a date, or an ISO date-time (UTC by default). */
export function asOfMs(value: string | Date): number {
  if (value instanceof Date) return value.getTime();
  const text = value.trim();
  if (/^\d+$/.test(text)) return Number(text);
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(text);
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(text);
  const parsed = Date.parse(
    dateOnly || zoned || !text.includes("T") ? text : text + "Z",
  );
  if (Number.isNaN(parsed)) throw new MemoryKingError("invalid_request");
  // A bare date means the end of that day: everything recorded on it counts.
  return dateOnly ? parsed + 86_399_999 : parsed;
}
