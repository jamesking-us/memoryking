import type { ViewerResponse, ViewerBinding } from "@memoryking/contracts";
import {
  Freshness,
  envelope,
  sameAuthority,
  retryDelay,
  type Clock,
  type Lease,
  type Ticket,
} from "./freshness.js";
import { HttpError, jsonRequest } from "./network.js";
import { ReportBuffer } from "./report.js";

export type Tab = "overview" | "tasks" | "memories" | "settings";
export interface Selection {
  tab: Tab;
  kind:
    | "overview"
    | "tasks"
    | "task"
    | "plans"
    | "plan"
    | "memories"
    | "memory"
    | "reports"
    | "report"
    | "graph"
    | "settings";
  id?: string;
  revision?: number;
  query?: string;
  cursor?: string;
  task?: string;
  phase?: string;
  plan?: string;
}
export interface State {
  status:
    "loading" | "ready" | "signed_out" | "unavailable" | "paused" | "limited";
  message: string;
  project: string;
  selection: Selection;
  session: ViewerResponse | null;
  response: ViewerResponse | null;
  report: ReportBuffer | null;
  figures: Map<string, string>;
  graphDetail: Selection | null;
  graphDetailResponse: ViewerResponse | null;
}
export interface Environment {
  clock: Clock;
  visible(): boolean;
  online(): boolean;
  request: typeof jsonRequest;
  timer(callback: () => void, ms: number): ReturnType<typeof setTimeout>;
  cancel(timer: ReturnType<typeof setTimeout>): void;
  random(): number;
  changed(state: State): void;
}
const home = (): Selection => ({ tab: "overview", kind: "overview" });
const initial = (): State => ({
  status: "loading",
  message: "Checking access…",
  project: "",
  selection: home(),
  session: null,
  response: null,
  report: null,
  figures: new Map(),
  graphDetail: null,
  graphDetailResponse: null,
});
const browserClock: Clock = {
  monotonic: () => performance.now(),
  wall: () => Date.now(),
};

export class ViewerClient {
  readonly gate: Freshness;
  state = initial();
  private abort = new AbortController();
  private poll: ReturnType<typeof setTimeout> | null = null;
  private expiry: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private retryAt = 0;
  private retryWall = 0;
  private nextPollAt = 0;
  private nextPollWall = 0;
  private signingOut = false;
  private figureLoading = false;
  readonly env: Environment;
  constructor(
    changed: Environment["changed"],
    environment: Partial<Environment> = {},
  ) {
    this.env = {
      clock: browserClock,
      visible: () => !document.hidden,
      online: () => navigator.onLine,
      request: jsonRequest,
      timer: (cb, ms) => setTimeout(cb, ms),
      cancel: (id) => clearTimeout(id),
      random: Math.random,
      changed,
      ...environment,
    };
    this.gate = new Freshness(this.env.clock, () => this.erase());
  }
  private cancelTimers(): void {
    if (this.poll !== null) this.env.cancel(this.poll);
    if (this.expiry !== null) this.env.cancel(this.expiry);
    this.poll = this.expiry = null;
  }
  private erase(): void {
    this.abort.abort();
    this.abort = new AbortController();
    this.cancelTimers();
    this.state.report?.clear();
    for (const url of this.state.figures.values()) URL.revokeObjectURL(url);
    this.state = initial();
    this.env.changed(this.state);
  }
  clear(message: string, status: State["status"] = "unavailable"): void {
    this.gate.clear();
    this.state.status = status;
    this.state.message = message;
    this.env.changed(this.state);
  }
  pause(): void {
    this.clear(
      "View paused. Access will be checked when you return.",
      "paused",
    );
  }
  resume(): void {
    this.clear("Checking access…", "loading");
    void this.refresh();
  }
  offline(): void {
    this.clear("Connection unavailable. Private content has been cleared.");
  }
  check(): boolean {
    if (this.state.status !== "ready") return false;
    if (!this.env.visible() || !this.env.online()) {
      this.pause();
      return false;
    }
    if (!this.gate.check()) {
      this.state.status = "unavailable";
      this.state.message = "View expired. A current access check is scheduled.";
      this.env.changed(this.state);
      this.schedule(
        Math.max(
          0,
          Math.min(
            this.nextPollAt - this.env.clock.monotonic(),
            this.nextPollWall - this.env.clock.wall(),
          ),
        ),
      );
      return false;
    }
    return true;
  }
  private schedule(delay: number): void {
    if (this.poll !== null) this.env.cancel(this.poll);
    if (!this.env.visible() || !this.env.online() || this.signingOut) return;
    this.nextPollAt = this.env.clock.monotonic() + delay;
    this.nextPollWall = this.env.clock.wall() + delay;
    // Long server Retry-After values must not overflow a browser's signed timer.
    this.poll = this.env.timer(
      () => {
        this.poll = null;
        void this.refresh();
      },
      Math.min(2147483647, Math.max(0, delay)),
    );
  }
  private armExpiry(): void {
    if (this.expiry !== null) this.env.cancel(this.expiry);
    this.expiry = this.env.timer(
      () => {
        this.expiry = null;
        this.check();
      },
      Math.max(1, this.gate.remaining()),
    );
  }
  private async read(
    path: string,
    kind: string,
    project: string,
    sequence: number,
    authority?: ViewerBinding,
  ): Promise<{ value: ViewerResponse; lease: Lease; ticket: Ticket }> {
    const ticket = this.gate.ticket(sequence);
    const controller = this.abort;
    const timeout = this.env.timer(() => controller.abort(), 5000);
    try {
      const raw = await this.env.request(path, controller.signal);
      const value = envelope(raw, kind, project);
      const lease = this.gate.receive(ticket, value.binding);
      if (
        authority &&
        (!sameAuthority(authority, value.binding) ||
          value.binding.observed_position < authority.observed_position)
      )
        throw new Error("context_changed");
      return { value, lease, ticket };
    } finally {
      this.env.cancel(timeout);
    }
  }
  private path(s = this.state.selection): string {
    const base = `/viewer/api/v1/projects/${encodeURIComponent(this.state.project)}/`;
    if (s.kind === "report")
      return `${base}reports/${encodeURIComponent(s.id ?? "")}/versions/${s.revision}`;
    if (s.kind === "task" || s.kind === "memory")
      return `${base}${s.kind === "task" ? "tasks" : "memories"}/${encodeURIComponent(s.id ?? "")}`;
    const query = new URLSearchParams();
    if (s.kind === "plan" && s.phase) query.set("phase", s.phase);
    if (s.kind === "memories" && s.query) query.set("q", s.query);
    if (s.cursor) query.set("cursor", s.cursor);
    const resource =
      s.kind === "plan" ? `plans/${encodeURIComponent(s.id ?? "")}` : s.kind;
    return base + resource + (query.size ? `?${query}` : "");
  }
  async refresh(): Promise<void> {
    if (!this.env.visible() || !this.env.online() || this.signingOut) return;
    const wait = Math.max(
      this.retryAt - this.env.clock.monotonic(),
      this.retryWall - this.env.clock.wall(),
    );
    if (wait > 0) {
      this.schedule(wait);
      return;
    }
    if (this.poll !== null) this.env.cancel(this.poll);
    this.poll = null;
    this.abort.abort();
    this.abort = new AbortController();
    const detail = this.state.graphDetail;
    const sequence = this.gate.startCycle(),
      generation = this.gate.generation,
      started = this.env.clock.monotonic();
    try {
      const session = await this.read(
        "/viewer/api/v1/session",
        "session",
        "_session",
        sequence,
      );
      if (session.value.data.kind !== "session")
        throw new Error("invalid_response");
      const old = this.state.session;
      if (
        old &&
        (!sameAuthority(old.binding, session.value.binding) ||
          old.data.kind !== "session" ||
          old.data.csrf !== session.value.data.csrf ||
          session.value.binding.observed_position <
            old.binding.observed_position)
      )
        throw new Error("context_changed");
      const projects = session.value.data.projects;
      const project = this.state.project || projects[0] || "";
      if (project && !projects.includes(project))
        throw new Error("context_changed");
      this.state.project = project;
      const leases = [session.lease];
      let result = session;
      if (project && this.state.selection.kind !== "settings") {
        result = await this.read(
          this.path(),
          this.state.selection.kind,
          project,
          sequence,
          session.value.binding,
        );
        leases.push(result.lease);
        if (
          old &&
          this.state.response &&
          result.value.binding.observed_position <
            this.state.response.binding.observed_position
        )
          throw new Error("context_changed");
      }
      // A graph drilldown retains the graph only while BOTH views are current.
      // Detail reads cannot renew a hidden graph's authority on their own.
      let detailResult: ViewerResponse | null = null;
      if (detail) {
        if (result.value.data.kind !== "graph")
          throw new Error("context_changed");
        const loaded = await this.read(
          this.path(detail),
          detail.kind,
          project,
          sequence,
          result.value.binding,
        );
        leases.push(loaded.lease);
        detailResult = loaded.value;
      }
      this.state.session = session.value;
      this.state.response =
        project && this.state.selection.kind !== "settings"
          ? result.value
          : null;
      if (result.value.data.kind === "report") {
        const metadata = result.value.data;
        if (!this.state.report)
          this.state.report = new ReportBuffer(metadata.sections);
        if (this.state.report.pages !== metadata.sections)
          throw new Error("invalid_report");
        // Immutable report metadata rechecks EVERY frozen dependency. Only that
        // whole-report check can renew retained, previously verified blocks/images.
        let count = 0;
        while (
          !this.state.report.complete &&
          count++ < 64 &&
          this.env.clock.monotonic() - started < 5000
        ) {
          const buffer = this.state.report;
          const suffix = buffer.cursor
            ? `?cursor=${encodeURIComponent(buffer.cursor)}`
            : "";
          const fragment = await this.read(
            `${this.path()}/sections/${buffer.page}${suffix}`,
            "report_section",
            project,
            sequence,
            result.value.binding,
          );
          if (fragment.value.data.kind !== "report_section")
            throw new Error("invalid_report");
          buffer.append(
            fragment.value.data.page,
            fragment.value.data.pages,
            fragment.value.data.content,
            fragment.value.next_cursor,
          );
          leases.push(fragment.lease);
        }
      }
      this.gate.commit(result.ticket, leases);
      this.state.graphDetailResponse = detailResult;
      if (
        detail &&
        detailResult?.data.kind === "memory" &&
        detailResult.data.memories.length === 1
      )
        detail.id = detailResult.data.memories[0].handle;
      if (
        this.state.selection.kind === "memory" &&
        result.value.data.kind === "memory" &&
        result.value.data.memories.length === 1
      ) {
        // Detail handles are short-lived; retain only the freshly authorized
        // replacement returned by this read, never an expired navigation handle.
        this.state.selection.id = result.value.data.memories[0].handle;
      }
      this.state.status = "ready";
      this.state.message = "Current permitted view";
      this.failures = 0;
      this.retryAt = this.retryWall = 0;
      if (!this.check()) return;
      this.env.changed(this.state);
      this.armExpiry();
      // Opening a full report is an explicit bounded sequence of section reads.
      // Between batches recheck session and whole-report authority; otherwise poll.
      this.schedule(
        this.state.report && !this.state.report.complete
          ? 0
          : Math.max(
              0,
              session.value.data.poll_seconds * 1000 -
                (this.env.clock.monotonic() - started),
            ),
      );
    } catch (error) {
      if (
        generation !== this.gate.generation ||
        sequence !== this.gate.sequence
      )
        return;
      const e = error instanceof HttpError ? error : null;
      const context =
        error instanceof Error && error.message === "context_changed";
      const invalidCursor = e?.code === "invalid_cursor";
      this.clear(
        e?.status === 401
          ? "Sign in to view your permitted projects."
          : e?.status === 404
            ? "This view is unavailable. Check your project access."
            : context || invalidCursor
              ? "Access or saved view changed. Reloading a current overview…"
              : e?.status === 429
                ? "Request limit reached. Retrying after the server's wait period."
                : "Connection or view unavailable. Private content has been cleared.",
        e?.status === 401
          ? "signed_out"
          : e?.status === 429
            ? "limited"
            : "unavailable",
      );
      if (e?.status === 401) return;
      const delay =
        (context || invalidCursor) && this.failures === 0
          ? 1000
          : retryDelay(
              this.failures,
              e?.retryAfter ?? null,
              this.env.random,
              this.env.clock.wall(),
            );
      this.failures++;
      this.retryAt = this.env.clock.monotonic() + delay;
      this.retryWall = this.env.clock.wall() + delay;
      this.schedule(delay);
    }
  }
  select(selection: Selection, project = this.state.project): void {
    if (!this.check()) return;
    this.clear("Loading current view…", "loading");
    this.state.project = project;
    this.state.selection = selection;
    void this.refresh();
  }
  openGraphDetail(selection: Selection): void {
    if (
      !this.check() ||
      this.state.selection.kind !== "graph" ||
      !["memory", "task", "plan"].includes(selection.kind)
    )
      return;
    this.state.graphDetail = { ...selection };
    this.state.graphDetailResponse = null;
    this.env.changed(this.state);
    void this.refresh();
  }
  backToGraph(): void {
    if (!this.check() || this.state.selection.kind !== "graph") return;
    this.state.graphDetail = null;
    this.state.graphDetailResponse = null;
    this.env.changed(this.state);
    // Supersede any in-flight detail response before it can restore the drilldown.
    void this.refresh();
  }
  async logout(all = false): Promise<void> {
    if (!this.check() || this.state.session?.data.kind !== "session") return;
    const csrf = this.state.session.data.csrf;
    this.signingOut = true;
    this.clear("Signing out…", "loading");
    const generation = this.gate.generation,
      controller = this.abort;
    const timeout = this.env.timer(() => controller.abort(), 5000);
    try {
      await this.env.request(
        `/viewer/auth/logout${all ? "-all" : ""}`,
        controller.signal,
        { csrf },
      );
      if (generation === this.gate.generation)
        this.clear("Signed out.", "signed_out");
    } catch {
      if (generation === this.gate.generation)
        this.clear(
          "Private content cleared. Sign-out could not be confirmed; reconnect before leaving a shared device.",
          "signed_out",
        );
    } finally {
      this.env.cancel(timeout);
      this.signingOut = false;
    }
  }
  async login(): Promise<string | null> {
    this.clear("Connecting to GitHub…", "loading");
    const generation = this.gate.generation,
      controller = this.abort;
    const timeout = this.env.timer(() => controller.abort(), 5000);
    try {
      const context = (await this.env.request(
        "/viewer/auth/context",
        controller.signal,
      )) as { schema_version: number; csrf: string };
      if (generation !== this.gate.generation || !this.env.visible())
        return null;
      if (
        context.schema_version !== 1 ||
        !/^[A-Za-z0-9_-]{32,128}$/.test(context.csrf)
      )
        throw new Error("invalid_response");
      const result = (await this.env.request(
        "/viewer/auth/github/start",
        controller.signal,
        { csrf: context.csrf },
      )) as { schema_version: number; authorization_url: string };
      if (generation !== this.gate.generation || !this.env.visible())
        return null;
      const url = new URL(result.authorization_url);
      if (
        result.schema_version !== 1 ||
        url.origin !== "https://github.com" ||
        url.pathname !== "/login/oauth/authorize" ||
        url.username ||
        url.password ||
        url.hash
      )
        throw new Error("invalid_response");
      return url.href;
    } catch {
      if (generation === this.gate.generation)
        this.clear(
          "Sign-in is unavailable. Check the connection or contact your operator.",
          "signed_out",
        );
      return null;
    } finally {
      this.env.cancel(timeout);
    }
  }
  async figure(name: string): Promise<void> {
    if (
      !this.check() ||
      this.figureLoading ||
      this.state.response?.data.kind !== "report" ||
      !/^[A-Za-z0-9_-]{1,96}\.(png|jpg|jpeg)$/.test(name)
    )
      return;
    if (this.state.figures.has(name)) return;
    const generation = this.gate.generation,
      sequence = this.gate.sequence;
    this.figureLoading = true;
    const binding = this.state.response.binding,
      path = this.path();
    const chunks: Uint8Array[] = [];
    let offset = 0,
      size = 0,
      hash = "";
    try {
      do {
        if (!this.check()) return;
        const part = await this.read(
          `${path}/images/${name}?offset=${offset}`,
          "report_image",
          this.state.project,
          sequence,
          binding,
        );
        if (!this.check() || generation !== this.gate.generation) return;
        const data = part.value.data;
        if (
          data.kind !== "report_image" ||
          data.name !== name ||
          data.offset !== offset ||
          !Number.isSafeInteger(data.bytes) ||
          data.bytes < 1 ||
          data.bytes > 8388608 ||
          !/^[a-f0-9]{64}$/.test(data.sha256) ||
          (offset > 0 && (hash !== data.sha256 || size !== data.bytes))
        )
          throw new Error("invalid_image");
        size = data.bytes;
        hash = data.sha256;
        const raw = Uint8Array.from(atob(data.data_base64), (c) =>
          c.charCodeAt(0),
        );
        if (
          !raw.length ||
          offset + raw.length > size ||
          (data.next_offset === null
            ? offset + raw.length !== size
            : data.next_offset !== offset + raw.length)
        )
          throw new Error("invalid_image");
        chunks.push(raw);
        offset += raw.length;
      } while (offset < size);
      const bytes = new Uint8Array(size);
      offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
        (x) => x.toString(16).padStart(2, "0"),
      ).join("");
      if (digest !== hash) throw new Error("invalid_image");
      if (
        generation !== this.gate.generation ||
        sequence !== this.gate.sequence ||
        !this.check()
      )
        return;
      this.state.figures.set(
        name,
        URL.createObjectURL(
          new Blob([bytes], {
            type: name.endsWith(".png") ? "image/png" : "image/jpeg",
          }),
        ),
      );
      this.env.changed(this.state);
    } catch {
      if (
        generation === this.gate.generation &&
        sequence === this.gate.sequence
      ) {
        this.clear("Figure unavailable. Reload to check current access.");
        this.schedule(10000);
      }
    } finally {
      chunks.length = 0;
      this.figureLoading = false;
    }
  }
}
