import type { Brief, RetrievalCommand } from "@memoryking/contracts";
import { readCredential, requireOneCredential } from "./credential.js";
import { MemoryKingError } from "./errors.js";
import { asOfMs, briefing, rawObject } from "./results.js";
import type { Briefing, Raw, RecallOptions } from "./types.js";

export interface LocalRecallOptions {
  /** The dashboard's loopback port (`memoryking serve --dashboard-port`). */
  port: number;
  project: string;
  /**
   * The store's history id, shown as `History` by `memoryking status`. A restore creates
   * a new history, and a client holding the old id is refused instead of reading it.
   */
  incarnation: string;
  /** The credential itself. Prefer `credentialFile`, which is re-read on every call. */
  credential?: string;
  /** A private (mode 0600) file holding the credential. */
  credentialFile?: string;
  /** Per-request limit in milliseconds. Default 10000. */
  timeoutMs?: number;
  /** A replacement for the global `fetch`. */
  fetch?: typeof fetch;
}

/**
 * Recall from a local daemon over its loopback agent endpoint (`POST /api/v1/retrieval`).
 *
 * That endpoint serves retrieval commands only, so this client has `recall` and nothing
 * else. Writing (`remember`, `saveProgress`, `handOff`) needs the shared service through
 * `MemoryKing` or the stdio MCP bridge.
 */
export class MemoryKingLocal {
  readonly project: string;
  readonly #options: LocalRecallOptions;

  constructor(options: LocalRecallOptions) {
    if (
      !Number.isInteger(options.port) ||
      options.port < 1 ||
      options.port > 65535
    ) {
      throw new MemoryKingError("invalid_request");
    }
    requireOneCredential(options);
    this.project = options.project;
    this.#options = options;
  }

  /** A cited briefing for what you are about to do. */
  async recall(query: string, options: RecallOptions = {}): Promise<Briefing> {
    const args: Brief = {
      project: this.project,
      query,
      bindings: [{}],
      max_output_tokens: options.budget ?? 6000,
      tokenizer: "utf8-byte-estimate-v1",
      format: options.format ?? "text",
    };
    if (options.task !== undefined) args.task = options.task;
    if (options.asOf !== undefined) args.as_of_ms = asOfMs(options.asOf);
    if (options.salience) args.salience = "on";
    if (options.rerank) args.rerank = "on";
    return briefing(
      await this.#post({
        version: 1,
        incarnation: this.#options.incarnation,
        action: "brief",
        args,
      }),
    );
  }

  async #post(command: RetrievalCommand): Promise<Raw> {
    let response: Response;
    try {
      response = await (this.#options.fetch ?? fetch)(
        `http://127.0.0.1:${this.#options.port}/api/v1/retrieval`,
        {
          method: "POST",
          redirect: "error",
          signal: AbortSignal.timeout(this.#options.timeoutMs ?? 10_000),
          headers: {
            Authorization: `Bearer ${readCredential(this.#options)}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(command),
        },
      );
    } catch (error) {
      if (error instanceof MemoryKingError) throw error;
      const timedOut = error instanceof Error && error.name === "TimeoutError";
      throw new MemoryKingError(
        timedOut ? "request_timeout" : "transport_unavailable",
      );
    }
    let value: Raw;
    try {
      value = rawObject(JSON.parse(await response.text()));
    } catch {
      throw new MemoryKingError("invalid_response", null, response.status);
    }
    if (typeof value.error === "string") {
      throw new MemoryKingError(
        value.error,
        typeof value.hint === "string" ? value.hint : null,
        response.status,
      );
    }
    if (!response.ok)
      throw new MemoryKingError("invalid_response", null, response.status);
    return value;
  }
}
