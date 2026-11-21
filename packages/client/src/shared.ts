import { createHash } from "node:crypto";
import { readCredential, requireOneCredential } from "./credential.js";
import { MemoryKingError } from "./errors.js";
import {
  briefing,
  handoff,
  progress,
  remembered,
  resumed,
  asOfMs,
  rawObject,
} from "./results.js";
import type {
  Briefing,
  HandOff,
  HandOffOptions,
  Progress,
  RecallOptions,
  Raw,
  RememberOptions,
  Remembered,
  ResumeOptions,
  Resumed,
  SaveProgressSections,
} from "./types.js";

const DAY_MS = 86_400_000;
const PROTOCOL_VERSION = "2025-11-25";
const LOOPBACK = new Set(["127.0.0.1", "[::1]", "localhost"]);
const CARRIED = [
  "next_actions",
  "hard_constraints",
  "findings",
  "failed_attempts",
  "blockers",
] as const;

export interface MemoryKingOptions {
  /** The service's base URL, such as `https://memory.example.com`. */
  url: string;
  tenant: string;
  project: string;
  /** The credential itself. Prefer `credentialFile`, which is re-read on every call. */
  credential?: string;
  /** A private (mode 0600) file holding the credential. */
  credentialFile?: string;
  /** Allow plain `http` to a loopback address, for local development only. */
  loopbackHttp?: boolean;
  /** Per-request limit in milliseconds. Default 10000. */
  timeoutMs?: number;
  /** A replacement for the global `fetch`, for proxies or instrumentation. */
  fetch?: typeof fetch;
}

/**
 * A client for the shared MemoryKing service.
 *
 * Every method calls the service's Streamable-HTTP MCP endpoint, so authorization,
 * receipts, idempotency and budgets are those of the MCP tools. Mutations are never
 * retried automatically and redirects are refused. A private certificate authority is
 * trusted through Node's `NODE_EXTRA_CA_CERTS`.
 */
export class MemoryKing {
  readonly project: string;
  readonly #endpoint: string;
  readonly #options: MemoryKingOptions;
  #principal: string | null = null;

  constructor(options: MemoryKingOptions) {
    let parsed: URL;
    try {
      parsed = new URL(options.url);
    } catch {
      throw new MemoryKingError("invalid_shared_url");
    }
    if (
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      !["", "/"].includes(parsed.pathname)
    ) {
      throw new MemoryKingError("invalid_shared_url");
    }
    const loopback = LOOPBACK.has(parsed.hostname);
    if (
      parsed.protocol !== "https:" &&
      !(
        options.loopbackHttp === true &&
        parsed.protocol === "http:" &&
        loopback
      )
    ) {
      throw new MemoryKingError("shared_tls_required");
    }
    if (
      !/^[A-Za-z0-9_-]{1,96}$/.test(options.tenant) ||
      !/^[A-Za-z0-9_-]{1,96}$/.test(options.project)
    ) {
      throw new MemoryKingError("invalid_request");
    }
    requireOneCredential(options);
    this.project = options.project;
    this.#options = options;
    // The client needs the full catalog (its tool names, canonical-JSON replies and
    // raw envelopes), so it asks for it by name rather than relying on the endpoint's
    // default profile, which is the lean one.
    this.#endpoint = `${parsed.origin}/v1/tenants/${options.tenant}/projects/${options.project}/mcp?profile=full`;
  }

  /**
   * A client from the environment: `MEMORYKING_URL`, `MEMORYKING_TENANT`,
   * `MEMORYKING_PROJECT` and `MEMORYKING_CREDENTIAL` (the path of a private file).
   * Anything in `overrides` wins.
   */
  static connect(
    overrides: Partial<MemoryKingOptions> = {},
    environment: Record<string, string | undefined> = process.env,
  ): MemoryKing {
    const url = overrides.url ?? environment.MEMORYKING_URL;
    const tenant = overrides.tenant ?? environment.MEMORYKING_TENANT;
    const project = overrides.project ?? environment.MEMORYKING_PROJECT;
    const file = environment.MEMORYKING_CREDENTIAL;
    if (!url) throw new MemoryKingError("shared_url_required");
    if (!tenant) throw new MemoryKingError("tenant_required");
    if (!project) throw new MemoryKingError("project_required");
    const credential =
      overrides.credential === undefined &&
      overrides.credentialFile === undefined
        ? file
          ? { credentialFile: file }
          : {}
        : {};
    return new MemoryKing({
      ...credential,
      ...overrides,
      url,
      tenant,
      project,
    });
  }

  /** The caller's identity and grant for this project. */
  async context(): Promise<Raw> {
    const reply = await this.#tool("memory_status", { project: this.project });
    if (typeof reply.principal === "string") this.#principal = reply.principal;
    return reply;
  }

  /** Save one observation as evidence. Pass a stable `event` to make retries replay. */
  async remember(
    text: string,
    options: RememberOptions = {},
  ): Promise<Remembered> {
    const source = options.source ?? "agent";
    // Replay needs identical bytes, so the default observed time is the day, and the
    // derived event id folds the day in; the store keeps the exact time of the capture.
    const observed =
      options.observedMs ?? Math.floor(Date.now() / DAY_MS) * DAY_MS;
    const event =
      options.event ??
      "ev-" +
        createHash("sha256")
          .update(`${source}\n${this.project}\n${observed}\n${text}`)
          .digest("hex")
          .slice(0, 40);
    return remembered(
      await this.#tool("memory_capture", {
        key: event,
        request: {
          source,
          source_event: event,
          text,
          readers: options.readers ?? null,
          classification: options.classification ?? "internal",
          observed_at_ms: observed,
        },
      }),
    );
  }

  /** A cited briefing for what you are about to do. */
  async recall(query: string, options: RecallOptions = {}): Promise<Briefing> {
    const args: Record<string, unknown> = {
      query,
      max_output_tokens: options.budget ?? 6000,
      format: options.format ?? "text",
    };
    if (options.task !== undefined) args.task = options.task;
    if (options.asOf !== undefined) args.as_of_ms = asOfMs(options.asOf);
    if (options.salience) args.salience = "on";
    if (options.rerank) args.rerank = "on";
    return briefing(
      await this.#tool("memory_brief", { action: "brief", args }),
    );
  }

  /** Save where a task stands. A section left out keeps the previous save's content. */
  async saveProgress(
    task: string,
    objective: string,
    currentState: string,
    sections: SaveProgressSections = {},
  ): Promise<Progress> {
    if (this.#principal === null) await this.context();
    const principal = this.#principal as string;
    const checkpoint: Record<string, unknown> = {
      objective,
      current_state: currentState,
    };
    if (sections.nextActions) checkpoint.next_actions = sections.nextActions;
    if (sections.hardConstraints)
      checkpoint.hard_constraints = sections.hardConstraints;
    if (sections.findings) {
      checkpoint.findings = sections.findings.map((text) => ({
        text,
        kind: "hypothesis",
        evidence: [],
      }));
    }
    if (sections.failedAttempts) {
      checkpoint.failed_attempts = sections.failedAttempts.map((item) => ({
        action: item.action,
        outcome: item.outcome,
        do_not_repeat_when:
          item.do_not_repeat_when ?? "conditions are unchanged",
      }));
    }
    if (sections.blockers) {
      checkpoint.blockers = sections.blockers.map((text) => ({
        text,
        owner: principal,
      }));
    }
    const run = sections.run ?? null;
    const save: Record<string, unknown> = { task, run, checkpoint };
    const previous = await this.#latest(task, run);
    if (previous !== null) {
      // Sections the caller left out carry over; sections it sent replace them.
      for (const section of CARRIED) {
        const kept = previous.state[section];
        if (
          !(section in checkpoint) &&
          Array.isArray(kept) &&
          kept.length > 0
        ) {
          checkpoint[section] = kept;
        }
      }
      save.expected_revision = previous.revision;
    }
    if (sections.shareWith && sections.shareWith.length > 0) {
      save.readers = [...new Set([principal, ...sections.shareWith])].sort();
    }
    return progress(
      await this.#tool("memory_checkpoint", { action: "save", args: save }),
    );
  }

  /** The handover note for a task's latest saved progress. */
  async resume(task: string, options: ResumeOptions = {}): Promise<Resumed> {
    return resumed(
      await this.#tool("memory_resume", {
        action: "resume",
        args: {
          task,
          run: options.run ?? null,
          checkpoint: null,
          max_output_tokens: options.budget ?? 12000,
          format: "text",
        },
      }),
    );
  }

  /** Prepare a task's latest progress for another principal, under their permissions. */
  async handOff(
    task: string,
    to: string,
    options: HandOffOptions = {},
  ): Promise<HandOff> {
    const run = options.run ?? null;
    const latest = await this.#tool("memory_resume", {
      action: "resume",
      args: { task, run, checkpoint: null, max_output_tokens: 2048 },
    });
    const checkpoint = latest.checkpoint as { id?: string } | null | undefined;
    // A blocked resume can still carry the caller's own checkpoint; a handoff needs the
    // constraints resolved, so it refuses.
    if (!checkpoint?.id || latest.status === "blocked") {
      throw new MemoryKingError("checkpoint_unavailable");
    }
    return handoff(
      await this.#tool("memory_handoff", {
        action: "prepare",
        args: {
          task,
          run,
          checkpoint: checkpoint.id,
          recipient: to,
          role: options.role ?? "review",
          stage: options.stage ?? "handoff",
          ttl_ms: (options.ttlMinutes ?? 60) * 60_000,
        },
      }),
    );
  }

  async #latest(
    task: string,
    run: string | null,
  ): Promise<{ revision: number; state: Record<string, unknown> } | null> {
    let packet: Raw;
    try {
      packet = await this.#tool("memory_resume", {
        action: "resume",
        args: {
          task,
          run,
          checkpoint: null,
          max_output_tokens: 65536,
          format: "json",
        },
      });
    } catch (error) {
      if (
        error instanceof MemoryKingError &&
        error.code === "checkpoint_unavailable"
      ) {
        return null;
      }
      throw error;
    }
    const checkpoint = packet.checkpoint as
      { revision: number; state: Record<string, unknown> } | null | undefined;
    // A stream that exists but cannot be read back refuses the save: replacing it blind
    // would erase what the caller did not repeat.
    if (!checkpoint) {
      throw new MemoryKingError(
        String(packet.reason ?? "checkpoint_unavailable"),
      );
    }
    return { revision: checkpoint.revision, state: checkpoint.state };
  }

  async #tool(name: string, args: Record<string, unknown>): Promise<Raw> {
    const body = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name, arguments: args },
    });
    if (Buffer.byteLength(body) > 262144)
      throw new MemoryKingError("invalid_request");
    let response: Response;
    try {
      response = await (this.#options.fetch ?? fetch)(this.#endpoint, {
        method: "POST",
        redirect: "error",
        signal: AbortSignal.timeout(this.#options.timeoutMs ?? 10_000),
        headers: {
          Authorization: `Bearer ${readCredential(this.#options)}`,
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
          "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        body,
      });
    } catch (error) {
      if (error instanceof MemoryKingError) throw error;
      const timedOut = error instanceof Error && error.name === "TimeoutError";
      throw new MemoryKingError(
        timedOut ? "request_timeout" : "transport_unavailable",
      );
    }
    let text: string;
    try {
      text = await response.text();
    } catch {
      throw new MemoryKingError("transport_unavailable");
    }
    if (text.length > 1_048_576) throw new MemoryKingError("invalid_response");
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      throw new MemoryKingError("invalid_response", null, response.status);
    }
    const reply = rawObject(value);
    if (typeof reply.error === "string") {
      throw new MemoryKingError(reply.error, hintOf(reply), response.status);
    }
    if (reply.error !== undefined || !response.ok) {
      throw new MemoryKingError("invalid_response", null, response.status);
    }
    const result = rawObject(reply.result);
    const content = result.content;
    const first = Array.isArray(content) ? rawObject(content[0]) : {};
    if (typeof first.text !== "string") {
      throw new MemoryKingError("invalid_response", null, response.status);
    }
    let payload: Raw;
    try {
      payload = rawObject(JSON.parse(first.text));
    } catch {
      // The MCP layer's own argument check answers in plain text.
      if (result.isError === true) {
        throw new MemoryKingError(
          "invalid_tool_arguments",
          first.text.slice(0, 200),
          200,
        );
      }
      throw new MemoryKingError("invalid_response", null, response.status);
    }
    if (result.isError === true) {
      throw new MemoryKingError(
        typeof payload.error === "string" ? payload.error : "invalid_response",
        hintOf(payload),
        response.status,
      );
    }
    return payload;
  }
}

function hintOf(value: Raw): string | null {
  return typeof value.hint === "string" ? value.hint : null;
}
