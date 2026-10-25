import { AtlasView, type GraphData } from "./graph.js";

type Doc = Record<string, unknown>;
export interface AuthContext {
  csrf: string;
  tenant: string;
  principal: string;
  incarnation: string;
  authorization_epoch?: number;
}
type Request = (path: string, body?: unknown) => Promise<Doc>;
const e = <K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text = "",
  cls = "",
): HTMLElementTagNameMap[K] => {
  const n = document.createElement(tag);
  n.textContent = text;
  n.className = cls;
  return n;
};
const doc = (v: unknown): Doc =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Doc) : {};
const rows = (v: unknown): Doc[] => (Array.isArray(v) ? v.map(doc) : []);
const readable = (v: unknown): string =>
  v === null || v === undefined
    ? "Unknown"
    : typeof v === "object"
      ? JSON.stringify(v)
      : String(v);
function detail(title: string, value: unknown): HTMLDetailsElement {
  const d = e("details");
  d.append(e("summary", title), e("pre", JSON.stringify(value, null, 2)));
  return d;
}
function button(text: string, action: () => void): HTMLButtonElement {
  const b = e("button", text);
  b.type = "button";
  b.onclick = action;
  return b;
}
function field(
  parent: HTMLElement,
  title: string,
  value = "",
  kind = "text",
): HTMLInputElement {
  const label = e("label", title),
    input = e("input");
  input.type = kind;
  input.value = value;
  input.autocomplete = "off";
  input.maxLength = 256;
  label.append(input);
  parent.append(label);
  return input;
}
function table(items: Doc[], keys: string[]): HTMLElement {
  const wrap = e("div", "", "table-scroll"),
    t = e("table"),
    h = e("tr");
  for (const k of keys) h.append(e("th", k.replaceAll("_", " ")));
  t.append(h);
  for (const row of items) {
    const tr = e("tr");
    for (const k of keys) tr.append(e("td", readable(row[k])));
    t.append(tr);
  }
  wrap.append(t);
  if (!items.length)
    wrap.append(e("p", "No authorized records in this window."));
  return wrap;
}

/** In-memory views are short lived and bound to the exact browser authority. */
export class Observatory {
  private root: HTMLElement;
  private status: HTMLElement;
  private output: HTMLElement;
  private project: HTMLInputElement;
  private bindings: HTMLTextAreaElement;
  private view: HTMLSelectElement;
  private ticket = 0;
  private expires = 0;
  private graph: AtlasView | null = null;
  private context = "";
  private confirmed: Doc | null = null;
  private writing = false;
  private lastRead: (() => Promise<Doc>) | null = null;
  constructor(
    root: HTMLElement,
    private api: Request,
    private current: () => AuthContext | null,
  ) {
    this.root = root;
    root.append(
      e("div", "EXPLORE · VERIFY · DECIDE", "eyebrow"),
      e("h2", "Memory observatory"),
      e(
        "p",
        "Inspect current authorized records. Historical views recheck current permission. Loaded snapshots expire after 15 seconds; refresh before acting. No model calls or agent scheduling occur here.",
      ),
    );
    const form = e("form", "", "observatory-form");
    this.project = field(form, "Project", "", "text");
    this.project.required = true;
    this.project.pattern = "[A-Za-z0-9_-]{1,96}";
    const label = e("label", "View");
    this.view = e("select");
    this.view.setAttribute("aria-label", "View");
    for (const [id, name] of [
      ["atlas", "Memory atlas"],
      ["history", "Checkpoint time machine"],
      ["review", "Review & conflicts"],
      ["cost", "Cost & efficiency"],
      ["operations", "Jobs & health"],
      ["handoffs", "Handoffs & checkpoints"],
      ["impact", "Change impact"],
      ["retention", "Retention & purge"],
    ]) {
      const o = e("option", name);
      o.value = id;
      this.view.append(o);
    }
    label.append(this.view);
    form.append(label);
    const advanced = e("details"),
      bl = e("label", "Applicability bindings (JSON array)");
    this.bindings = e("textarea");
    this.bindings.value = "[{}]";
    this.bindings.rows = 3;
    this.bindings.maxLength = 16384;
    bl.append(this.bindings);
    advanced.append(
      e("summary", "Task and environment scope"),
      bl,
      e(
        "p",
        "Supply known dimensions such as path, environment or version. Missing dimensions remain unknown and may block constraints; they are never treated as permission.",
        "caption",
      ),
    );
    form.append(advanced, e("button", "Load current view", "primary"));
    this.status = e("p", "Choose a project and view.");
    this.status.setAttribute("role", "status");
    this.output = e("div", "", "observatory-output");
    root.append(form, this.status, this.output);
    for (const input of [this.project, this.bindings, this.view])
      input.addEventListener("input", () =>
        this.clearResult("Scope changed. Load again to check current access."),
      );
    form.onsubmit = (ev) => {
      ev.preventDefault();
      void this.load();
    };
  }
  clear(): void {
    this.clearResult("Session changed. Reconnect and select a project.");
    this.project.value = "";
    this.bindings.value = "[{}]";
    this.context = "";
  }
  private clearResult(message: string): void {
    this.ticket++;
    this.expires = 0;
    this.confirmed = null;
    this.lastRead = null;
    this.graph?.clear();
    this.graph = null;
    this.output.replaceChildren();
    this.status.textContent = message;
  }
  tick(): void {
    const ctx = this.current();
    if (this.context && (!ctx || JSON.stringify(ctx) !== this.context)) {
      this.clear();
      return;
    }
    if (this.expires && Date.now() >= this.expires)
      this.clearResult(
        "Snapshot expired. Load again to recheck permission and source availability.",
      );
  }
  private scope(): Doc[] {
    const b: unknown = JSON.parse(this.bindings.value);
    if (
      !Array.isArray(b) ||
      !b.length ||
      b.length > 256 ||
      b.some((x) => !x || typeof x !== "object" || Array.isArray(x))
    )
      throw Error("Bindings must be a bounded array of objects");
    return b as Doc[];
  }
  private async command(
    operation: string,
    action: string,
    args: Doc = {},
    key?: string,
  ): Promise<Doc> {
    const ctx = this.current();
    if (!ctx || document.hidden)
      throw Error("Current visible session required");
    const stamp = JSON.stringify(ctx),
      ticket = this.ticket,
      project = this.project.value,
      started = Date.now();
    const command: Doc = {
      version: 1,
      incarnation: ctx.incarnation,
      action,
      args: { ...args, project },
    };
    if (key) command.key = key;
    const result = await this.api("/api/command", {
      csrf: ctx.csrf,
      operation,
      command,
    });
    if (
      ticket !== this.ticket ||
      JSON.stringify(this.current()) !== stamp ||
      project !== this.project.value ||
      document.hidden ||
      Date.now() >= started + 15000
    )
      throw Error("View changed while checking access");
    this.context = stamp;
    const deadline =
      typeof result.fresh_until_ms === "number"
        ? result.fresh_until_ms
        : started + 15000;
    this.expires = Math.min(
      this.expires || Infinity,
      started + 15000,
      deadline,
    );
    if (Date.now() >= this.expires) throw Error("Snapshot expired");
    this.status.textContent = `Current permission checked · ${project} · ${ctx.tenant} / ${ctx.principal} · history ${ctx.incarnation}. Snapshot valid for at most 15 seconds.`;
    return result;
  }
  private async run(action: () => Promise<void>): Promise<void> {
    const ticket = this.ticket;
    try {
      await action();
    } catch (error) {
      if (ticket === this.ticket) {
        this.clearResult(
          "Unavailable: " +
            (error instanceof Error
              ? error.message.replaceAll("_", " ")
              : "request failed") +
            ". Reload to reconcile; mutations are never automatically retried.",
        );
      }
    }
  }
  private async load(): Promise<void> {
    const started = performance.now();
    this.clearResult("Checking current permission…");
    await this.run(async () => {
      switch (this.view.value) {
        case "atlas":
          await this.atlas();
          break;
        case "history":
          await this.history();
          break;
        case "review":
          await this.review();
          break;
        case "cost":
          await this.cost();
          break;
        case "operations":
          await this.operations();
          break;
        case "handoffs":
          await this.handoffs();
          break;
        case "impact":
          this.impact();
          break;
        case "retention":
          this.retention();
          break;
      }
    });
    requestAnimationFrame(() => {
      this.root.dataset.viewReadyMs = String(
        Math.round(performance.now() - started),
      );
    });
  }
  private exportCurrent(load: () => Promise<Doc>): void {
    this.lastRead = load;
    const exportButton = button(
      "Export current authorized JSON",
      () =>
        void this.run(async () => {
          // Re-fetch through the same authorization gate; never serialize a stale DOM cache.
          if (!this.lastRead) throw Error("Reload required");
          const value = await this.lastRead(),
            url = URL.createObjectURL(
              new Blob(
                [
                  JSON.stringify(
                    {
                      exported_ms: Date.now(),
                      authorization: "current_at_export",
                      data: value,
                    },
                    null,
                    2,
                  ),
                ],
                { type: "application/json" },
              ),
            );
          const a = e("a");
          a.href = url;
          a.download = "memoryking-authorized-view.json";
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 0);
        }),
    );
    exportButton.dataset.export = "true";
    this.output.append(exportButton);
  }
  private async atlas(focus?: string, cursor?: string): Promise<void> {
    const args: Doc = {
      bindings: this.scope(),
      max_nodes: focus ? 500 : 200,
      max_edges: focus ? 1500 : 400,
    };
    if (focus) args.focus = [focus];
    if (cursor) args.cursor = cursor;
    const load = () => this.command("retrieval", "atlas", args),
      data = await load();
    if (!this.graph) {
      this.output.replaceChildren(e("h3", "Relationships with provenance"));
      const host = e("div");
      this.output.append(host);
      this.graph = new AtlasView(
        host,
        (id) => void this.run(() => this.atlas(id)),
      );
    }
    this.graph.set(data as unknown as GraphData);
    this.output.querySelector(".atlas-footer")?.remove();
    this.output.querySelector("[data-export]")?.remove();
    const footer = e("div", "", "atlas-footer");
    if (data.next_cursor)
      footer.append(
        button(
          "Next bounded page",
          () =>
            void this.run(() => this.atlas(focus, String(data.next_cursor))),
        ),
      );
    if (focus)
      footer.append(
        button("Return to overview", () => void this.run(() => this.atlas())),
      );
    this.output.append(footer);
    this.exportCurrent(load);
  }
  private async history(position?: number, baseline?: number): Promise<void> {
    const load = () =>
      this.command(
        "inspection",
        "history",
        position === undefined ? {} : { recorded_position: position },
      );
    const data = await load();
    this.output.replaceChildren(
      e(
        "h3",
        position === undefined
          ? "Live checkpoint state"
          : `Historical state at position ${position}`,
      ),
    );
    this.output.append(
      e(
        "p",
        "Current permissions apply to every frame. Moving through history never invokes a tool, replays a mutation or changes live task state. Operation history and historical liveness are unavailable.",
        "integrity",
      ),
    );
    const frames = rows(data.frames),
      controls = e("div", "", "history-controls");
    const pick = e("select");
    pick.setAttribute("aria-label", "Historical checkpoint");
    for (const f of frames) {
      const o = e(
        "option",
        `${f.task} · r${f.revision} · ${new Date(Number(f.recorded_ms)).toLocaleString()}`,
      );
      o.value = String(f.position);
      pick.append(o);
    }
    if (position !== undefined) pick.value = String(position);
    const base = e("select");
    base.setAttribute("aria-label", "Comparison baseline");
    base.append(e("option", "No comparison"));
    base.options[0]!.value = "";
    for (const f of frames) {
      const o = e("option", `${f.task} · position ${f.position}`);
      o.value = String(f.position);
      base.append(o);
    }
    if (baseline !== undefined) base.value = String(baseline);
    controls.append(
      pick,
      base,
      button(
        "Inspect selected frame",
        () =>
          void this.run(() =>
            this.history(
              Number(pick.value),
              base.value ? Number(base.value) : undefined,
            ),
          ),
      ),
      button("Return to live", () => void this.run(() => this.history())),
    );
    this.output.append(controls);
    if (frames.length) {
      const slider = e("input");
      slider.type = "range";
      slider.min = "0";
      slider.max = String(frames.length - 1);
      slider.value = String(
        Math.max(
          0,
          frames.findIndex((f) => f.position === position),
        ),
      );
      slider.setAttribute("aria-label", "Checkpoint timeline");
      slider.onchange = () => {
        const next = Number(frames[Number(slider.value)]!.position);
        void this.run(() =>
          this.history(next, base.value ? Number(base.value) : undefined),
        );
      };
      this.output.append(slider);
    } else this.output.append(e("p", "No currently accessible checkpoints."));
    const projection = doc(data.projection),
      tasks = rows(projection.tasks);
    this.output.append(this.taskTable(tasks));
    if (baseline !== undefined) {
      const previous = await this.command("inspection", "history", {
          recorded_position: baseline,
        }),
        before = rows(doc(previous.projection).tasks),
        changes: Doc[] = [];
      const all = [
        ...tasks,
        ...before.filter(
          (old) => !tasks.some((t) => t.task === old.task && t.run === old.run),
        ),
      ];
      for (const task of all) {
        const current = tasks.find(
          (t) => t.task === task.task && t.run === task.run,
        );
        const old = before.find(
            (t) => t.task === task.task && t.run === task.run,
          ),
          p = doc(current?.progress),
          b = doc(old?.progress);
        changes.push({
          task: task.task,
          run: task.run,
          baseline: baseline,
          scope_changed: !current
            ? "absent from selected bounded frame"
            : old
              ? b.scope_hash !== p.scope_hash
              : "no baseline",
          passed_before: b.passed,
          passed_now: p.passed,
          total_before: b.total,
          total_now: p.total,
        });
      }
      this.output.append(
        e("h3", "Scope-aware comparison"),
        e(
          "p",
          "Changed denominators reflect scope edits. A higher fraction alone is not evidence of progress.",
        ),
        table(changes, [
          "task",
          "run",
          "scope_changed",
          "passed_before",
          "passed_now",
          "total_before",
          "total_now",
        ]),
      );
    }
    this.output.append(detail("Full authorized historical projection", data));
    this.exportCurrent(load);
  }
  private taskTable(tasks: Doc[]): HTMLElement {
    return table(
      tasks.map((t) => ({
        ...t,
        passed: doc(t.progress).passed,
        total: doc(t.progress).total,
        heartbeat: doc(t.heartbeat).status,
      })),
      ["task", "run", "revision", "writer", "passed", "total", "heartbeat"],
    );
  }
  private async review(): Promise<void> {
    const bindings = this.scope();
    const knowledge = await this.command("knowledge", "query", {
      bindings,
      diagnostic: true,
    });
    const constraints = await this.command("knowledge", "constraints", {
      bindings,
    });
    const procedures = await this.command("experience", "query", {
      bindings,
      diagnostic: true,
    });
    this.output.replaceChildren(
      e("h3", "Review queue & evidence quality"),
      e(
        "p",
        "Source content is untrusted evidence. Review changes lifecycle authority; procedure activation is separate and grants no execution privileges. A policy change requires a new browser sign-in.",
        "integrity",
      ),
    );
    for (const [name, data, operation] of [
      ["Knowledge", knowledge, "knowledge"],
      ["Constraints", constraints, "knowledge"],
      ["Procedures", procedures, "experience"],
    ] as const) {
      const section = e("section");
      section.append(
        e("h3", name),
        e(
          "p",
          `Resolution: ${readable(data.status)} · ${readable(data.reason)}`,
        ),
      );
      for (const item of rows(data.items)) {
        const card = e("article", "", "review-card"),
          body = doc(item.body);
        card.append(
          e("h4", String(body.text ?? body.name ?? item.revision)),
          e(
            "p",
            `${readable(item.state ?? item.lifecycle)} · ${readable(item.authority)} · version ${item.family_version}`,
          ),
          detail("Applicability, sources, conflicts and attestations", item),
        );
        const controls = e("div", "", "actions");
        for (const decision of [
          "accept",
          "reject",
          "deprecate",
          "quarantine",
          "release_quarantine",
        ])
          controls.append(
            button(decision.replaceAll("_", " "), () =>
              this.reviewAction(operation, "review", item, {
                decision,
                breadth_reviewed: false,
              }),
            ),
          );
        if (operation === "experience") {
          controls.append(
            button("Activate", () =>
              this.reviewAction(operation, "activate", item, {
                bindings,
                policy: "reviewed-procedure-v1",
              }),
            ),
            button("Suspend", () =>
              this.reviewAction(operation, "suspend", item, {}),
            ),
          );
        }
        card.append(controls);
        section.append(card);
      }
      if (!rows(data.items).length)
        section.append(e("p", "No authorized matching revisions."));
      section.append(detail("Resolution and completeness details", data));
      this.output.append(section);
    }
    const ranking = await this.command("experience", "ranking_inspect");
    this.output.append(
      detail("Active ranking and evaluation evidence", ranking),
    );
    this.composer();
  }
  private reviewAction(
    operation: string,
    action: string,
    item: Doc,
    extra: Doc,
  ): void {
    const revision = item.revision,
      expected_version = item.family_version;
    const actionLabel = String(extra.decision ?? action).replaceAll("_", " ");
    const form = e("form", "", "review-confirm");
    form.append(
      e("h3", `${actionLabel} revision ${revision}`),
      e(
        "p",
        `Compare-and-set family version ${expected_version}. Check the source and scope above before confirming. The server rechecks current authority and version.`,
      ),
    );
    const reason = field(
      form,
      "Decision reason (identifier)",
      "reviewed_in_dashboard",
    );
    reason.required = true;
    reason.pattern = "[A-Za-z0-9_-]{1,96}";
    const breadth = field(
      form,
      "I reviewed the applicability breadth",
      "",
      "checkbox",
    );
    form.append(
      e("button", "Confirm " + actionLabel, "primary"),
      button("Cancel", () => form.remove()),
    );
    form.onsubmit = (event) => {
      event.preventDefault();
      void this.run(async () => {
        const args: Doc = {
          revision,
          expected_version,
          ...extra,
          reason: reason.value,
        };
        if (action === "review") args.breadth_reviewed = breadth.checked;
        await this.mutate(operation, action, args);
      });
    };
    this.output.querySelector(".review-confirm")?.remove();
    this.output.append(form);
    reason.focus();
  }
  private async mutate(
    operation: string,
    action: string,
    args: Doc,
  ): Promise<void> {
    if (this.writing) throw Error("A command is already pending");
    this.writing = true;
    let result: Doc;
    try {
      result = await this.command(operation, action, args, crypto.randomUUID());
    } finally {
      this.writing = false;
    }
    this.graph?.clear();
    this.output.replaceChildren(
      e("h3", "Command acknowledged"),
      detail("Recorded result", result),
      e(
        "p",
        "Reload to reconcile current state. If this command changed authorization, sign in again. Acknowledgment does not approve a phase or start an agent.",
      ),
    );
  }
  private composer(): void {
    const d = e("details");
    d.append(
      e("summary", "Advanced proposal / attestation"),
      e(
        "p",
        "Paste the documented command arguments for a knowledge or constraint proposal, an attestation, or a revision review with its expected family version. The preview is inert; confirmation sends one validated command. No source text is executed.",
      ),
    );
    const select = e("select");
    select.setAttribute("aria-label", "Advanced command");
    for (const action of ["propose", "attest", "review"]) {
      const o = e("option", action);
      o.value = action;
      select.append(o);
    }
    const input = e("textarea");
    input.rows = 10;
    input.maxLength = 100000;
    input.setAttribute("aria-label", "Command arguments JSON");
    input.placeholder = "Documented arguments excluding project";
    const preview = e("div");
    input.oninput = () => {
      this.confirmed = null;
      preview.replaceChildren();
    };
    select.onchange = () => {
      this.confirmed = null;
      preview.replaceChildren();
    };
    d.append(
      select,
      input,
      button("Preview command", () => {
        try {
          const args: unknown = JSON.parse(input.value);
          if (!args || Array.isArray(args) || typeof args !== "object")
            throw Error("Object required");
          this.confirmed = {
            operation: "knowledge",
            action: select.value,
            args,
          };
          preview.replaceChildren(
            detail("Exact proposed change", {
              ...this.confirmed,
              project: this.project.value,
            }),
            button(
              "Confirm this command",
              () =>
                void this.run(async () => {
                  const c = this.confirmed;
                  if (!c) throw Error("Preview again");
                  this.confirmed = null;
                  await this.mutate(
                    String(c.operation),
                    String(c.action),
                    doc(c.args),
                  );
                }),
            ),
          );
        } catch {
          preview.replaceChildren(e("p", "Enter a valid JSON object."));
        }
      }),
      preview,
    );
    this.output.append(d);
  }
  private async cost(): Promise<void> {
    const load = () => this.command("inspection", "cost"),
      data = await load(),
      summary = doc(data.processing);
    this.output.replaceChildren(
      e("h3", "Every recorded attempt counts"),
      e(
        "p",
        "Known usage is a lower bound when any attempts are unknown. Failed calls, retries and background processing remain included. Provider reports and actor outcomes are not independent metering.",
        "integrity",
      ),
    );
    const metrics = e("div", "", "metrics");
    for (const [name, value] of [
      ["Attempts", summary.attempts],
      ["Total tokens", summary.total_tokens],
      ["Known token lower bound", summary.known_token_lower_bound],
      ["Unknown token attempts", summary.unknown_token_attempts],
      ["Full-task cost", data.full_task_cost],
      ["Savings", data.savings],
    ]) {
      const card = e("div", "", "metric");
      card.append(e("small", String(name)), e("strong", readable(value)));
      metrics.append(card);
    }
    this.output.append(metrics);
    const groups: Doc[] = [];
    for (const provider of rows(summary.providers))
      for (const [stage, value] of Object.entries(doc(provider.stages))) {
        const v = doc(value);
        groups.push({
          provider: provider.provider,
          model: provider.fingerprint,
          stage,
          attempts: v.attempts,
          known_tokens: v.known_token_lower_bound,
          unknown: v.unknown_token_attempts,
          cost: doc(v.cost_microusd).total,
        });
      }
    const chart = e("div", "", "cost-bars"),
      max = Math.max(1, ...groups.map((g) => Number(g.known_tokens)));
    for (const g of groups) {
      const row = e("div");
      row.append(
        e(
          "span",
          `${g.provider} · ${readable(g.stage)}: ≥${g.known_tokens} tokens (${g.unknown} unknown)`,
        ),
      );
      const meter = e("meter");
      meter.min = 0;
      meter.max = max;
      meter.value = Number(g.known_tokens);
      meter.setAttribute(
        "aria-label",
        `${readable(g.stage)} known token lower bound`,
      );
      row.append(meter);
      chart.append(row);
    }
    this.output.append(
      chart,
      table(groups, [
        "provider",
        "model",
        "stage",
        "attempts",
        "known_tokens",
        "unknown",
        "cost",
      ]),
      detail("All retained authorized attempts", data.attempts),
      detail("Actor-reported outcomes, separate from usage", data.outcomes),
      detail("Coverage and accounting uncertainty", data),
    );
    this.exportCurrent(load);
  }
  private async operations(): Promise<void> {
    const load = () => this.command("inspection", "operations"),
      data = await load();
    this.output.replaceChildren(
      e("h3", "Jobs & primary health"),
      e("p", readable(data.health)),
      table(rows(data.jobs), [
        "id",
        "state",
        "position",
        "attempts",
        "fence",
        "lease_until",
        "available",
        "age_ms",
      ]),
      detail("Projection frontiers, coverage and measured fields", data),
      e(
        "p",
        "Frontiers are opaque ordering tokens, not item counts. Lock wait, cache churn and recovery qualification remain unknown here. Retry, cancel and schedule controls are unavailable.",
        "integrity",
      ),
    );
    this.exportCurrent(load);
  }
  private async handoffs(): Promise<void> {
    const data = await this.command("inspection", "handoffs");
    this.output.replaceChildren(
      e("h3", "Handoff lifecycle"),
      e(
        "p",
        "Only handoffs involving this principal and a currently accessible checkpoint are shown. Role labels grant no authority. Preview uses the sender–recipient permission intersection; this screen never acknowledges work or changes the active writer.",
      ),
      table(rows(data.items), [
        "id",
        "task",
        "checkpoint",
        "sender",
        "recipient",
        "effective",
        "expires",
      ]),
      detail("Authorized handoff state", data),
    );
    const form = e("form", "", "observatory-form"),
      handle = field(form, "Handle"),
      action = e("select");
    action.setAttribute("aria-label", "Handoff action");
    for (const value of ["preview", "materialize", "cancel"]) {
      const option = e("option", value);
      option.value = value;
      action.append(option);
    }
    form.append(action, e("button", "Preview request"));
    const preview = e("div");
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const args: Doc = { handle: handle.value };
      if (action.value !== "cancel")
        Object.assign(args, {
          max_output_tokens: 8192,
          tokenizer: "cl100k_base",
        });
      const chosen = action.value;
      preview.replaceChildren(
        detail("Exact handoff request", { action: chosen, ...args }),
        button(
          "Confirm " + chosen,
          () => void this.run(() => this.mutate("handoff", chosen, args)),
        ),
      );
    };
    this.output.append(form, preview);
    const prepare = e("details");
    prepare.append(e("summary", "Prepare a handoff from a checkpoint"));
    const f = e("form", "", "observatory-form");
    const fields = Object.fromEntries(
      ["task", "run", "checkpoint", "recipient", "role", "stage"].map((k) => [
        k,
        field(f, k),
      ]),
    );
    const ttl = field(f, "Expires in milliseconds", "60000", "number");
    f.append(e("button", "Preview preparation"));
    const p = e("div");
    f.onsubmit = (ev) => {
      ev.preventDefault();
      const args: Doc = Object.fromEntries(
        Object.entries(fields).map(([k, v]) => [k, v.value || null]),
      );
      args.bindings = this.scope();
      args.ttl_ms = Number(ttl.value);
      p.replaceChildren(
        detail("Exact recipient and scope", args),
        button(
          "Confirm preparation",
          () => void this.run(() => this.mutate("handoff", "prepare", args)),
        ),
      );
    };
    prepare.append(f, p);
    this.output.append(prepare);
  }
  private impact(): void {
    this.status.textContent =
      "Enter the repository snapshot and paths to check current access.";
    this.output.replaceChildren(
      e("h3", "Code change impact"),
      e(
        "p",
        "Direct evidence links and bounded Python parser relations. Missing files or unsupported languages remain coverage gaps; absence of edges does not prove no impact.",
      ),
    );
    const f = e("form", "", "observatory-form"),
      repository = field(f, "Repository ID"),
      snapshot = field(f, "Snapshot ID"),
      paths = field(f, "Changed paths (comma separated)");
    f.append(e("button", "Inspect impact"));
    const result = e("div");
    f.onsubmit = (ev) => {
      ev.preventDefault();
      void this.run(async () => {
        const args = {
            repository: repository.value,
            snapshot: snapshot.value,
            paths: paths.value
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean),
          },
          value = await this.command("repository", "impact", {
            ...args,
            limit: 128,
          }),
          coverage = await this.command("repository", "coverage", {
            ...args,
            bindings: this.scope(),
          });
        result.replaceChildren(
          detail("Evidence and affected claims", value),
          detail("Parser and policy coverage", coverage),
        );
        const host = e("div");
        result.prepend(host);
        this.graph?.clear();
        this.graph = new AtlasView(host, () => {});
        this.graph.set({
          nodes: rows(value.nodes).map((n) => ({
            ...n,
            id: String(n.id),
            type: String(n.kind),
            label: String(n.id),
          })),
          edges: rows(value.edges).map((edge, i) => ({
            ...edge,
            id: "impact-edge-" + i,
            source: String(edge.source),
            target: String(edge.target),
            type: String(edge.kind),
            provenance: edge,
          })),
          coverage:
            "Direct current evidence dependencies; no similarity inference",
          truncation: value.truncated ? ["impact_limit"] : [],
        });
      });
    };
    this.output.append(f, result);
  }
  private retention(): void {
    this.status.textContent =
      "Enter an observation to authorize and preview its consequences.";
    this.output.replaceChildren(
      e("h3", "Preview retention consequences"),
      e(
        "p",
        "Permanent purge also invalidates dependent context, retry results, tenant experience bodies and ranking state. It cannot erase prior exports, provider deliveries or backups. Review the server preview before confirming.",
        "integrity",
      ),
    );
    const f = e("form", "", "observatory-form"),
      observation = field(f, "Observation ID");
    observation.required = true;
    f.append(e("button", "Preview purge"));
    const preview = e("div");
    observation.oninput = () => preview.replaceChildren();
    f.onsubmit = (ev) => {
      ev.preventDefault();
      void this.run(async () => {
        const id = observation.value,
          value = await this.command("inspection", "retention_preview", {
            observation: id,
          });
        preview.replaceChildren(
          detail(
            "Affected records, gaps and irreversible effects",
            Object.fromEntries(
              Object.entries(value).filter(([key]) => key !== "preview"),
            ),
          ),
        );
        const confirmForm = e("form"),
          confirm = field(confirmForm, `Type ${id} to confirm permanent purge`);
        confirm.required = true;
        confirmForm.append(e("button", "Permanently purge", "danger"));
        confirmForm.onsubmit = (event) => {
          event.preventDefault();
          void this.run(async () => {
            if (confirm.value !== id || observation.value !== id)
              throw Error("Confirmation does not match preview");
            await this.mutate("inspection", "purge", {
              observation: id,
              preview: value.preview,
              confirm: confirm.value,
            });
          });
        };
        preview.append(confirmForm);
      });
    };
    this.output.append(f, preview);
  }
}
