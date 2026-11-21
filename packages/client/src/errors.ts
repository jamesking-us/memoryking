/** An error the service reported, or a failure to reach it. */
export class MemoryKingError extends Error {
  /** The stable `error` code, such as `denied` or `checkpoint_unavailable`. */
  readonly code: string;
  /** The service's one-line advice for this code, when it sent one. */
  readonly hint: string | null;
  /** The HTTP status, when the failure came from an HTTP reply. */
  readonly status: number | null;

  constructor(
    code: string,
    hint: string | null = null,
    status: number | null = null,
  ) {
    super(hint === null ? code : `${code}: ${hint}`);
    this.name = "MemoryKingError";
    this.code = code;
    this.hint = hint;
    this.status = status;
  }
}
