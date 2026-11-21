/** One cited source of a briefing; `ref` is the `[g1]`-style label used in its text. */
export interface Source {
  id: string;
  kind: string;
  ref: string;
}

export interface FailedAttempt {
  action: string;
  outcome: string;
  do_not_repeat_when?: string;
}

/** The service's whole reply, for anything the typed result leaves out. */
export type Raw = Record<string, unknown>;

export interface Remembered {
  observation: string;
  position: number;
  held: boolean;
  raw: Raw;
}

export interface Briefing {
  /** Markdown for `format: "text"`, empty for `"json"`. */
  text: string;
  /** `ready`, `partial` or `blocked`; blocked means a decision is needed. */
  status: string;
  receipt: string;
  sources: Source[];
  warnings: string[];
  raw: Raw;
}

export interface Progress {
  checkpoint: string;
  revision: number;
  status: string;
  receipt: string;
  replayed: boolean;
  raw: Raw;
}

export interface Resumed {
  text: string;
  status: string;
  receipt: string;
  raw: Raw;
}

export interface HandOff {
  status: string;
  handle: string;
  raw: Raw;
}

export interface RememberOptions {
  /** Where it came from, such as `pytest`. Default `agent`. */
  source?: string;
  /** Stable id of the event, so a retry replays. Default: a hash of source and text. */
  event?: string;
  /** Principals allowed to read it; `null` means everyone in the project. */
  readers?: string[] | null;
  classification?: "public" | "internal" | "restricted";
  /** When it was observed, in Unix milliseconds. Default: today, to the day. */
  observedMs?: number;
}

export interface RecallOptions {
  /** Token budget for the briefing. Default 6000. */
  budget?: number;
  /** Brief as the project stood then: a Date, `YYYY-MM-DD` or an ISO date-time. */
  asOf?: string | Date;
  rerank?: boolean;
  salience?: boolean;
  task?: string;
  format?: "text" | "json";
}

export interface SaveProgressSections {
  nextActions?: string[];
  findings?: string[];
  failedAttempts?: FailedAttempt[];
  blockers?: string[];
  hardConstraints?: string[];
  run?: string | null;
  /** Other principals who may read this task's progress. */
  shareWith?: string[];
}

export interface ResumeOptions {
  run?: string | null;
  /** Token budget. Default 12000. */
  budget?: number;
}

export interface HandOffOptions {
  role?: "review" | "implement" | "verify" | "operate";
  stage?: string;
  ttlMinutes?: number;
  run?: string | null;
}
