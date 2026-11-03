import { Observatory } from "./observatory.js";
import { installEmblem } from "./brand.js";
import { Team } from "./team.js";
interface Evidence {
  report: string;
  verification: string;
  smtp: string;
  device: string;
  links: string[];
  code_revision?: string;
  platform?: string;
  source_hash?: string;
  checks?: { name: string; exit_code: number; duration_ms: number }[];
}
interface Phase {
  id: string;
  title: string;
  outcome: string;
  criteria: string[];
  execution: string;
  approval: string;
  approval_basis: string;
  evidence: Evidence;
}
interface Snapshot {
  schema_version: number;
  plan_id: string;
  plan_revision: string;
  source_hash: string;
  register_hash: string;
  observed_ms: number;
  fresh_until_ms: number;
  adapter: string;
  phases: Phase[];
  counts: {
    closed: number;
    report_closeouts: number;
    total: number;
    historical_jobs_passed: number;
  };
  adrs: { id: string; url: string }[];
  csrf: string;
  incarnation: string;
  principal: string;
  tenant: string;
  authorization_epoch?: number;
  capacity?: Record<string, { used: number; limit: number; warning: boolean }>;
  streaming?: boolean;
  stream_sequence?: number;
  resync_required?: boolean;
}
const CAPACITY_LABELS: Record<string, [string, string]> = {
  jobs: ["Pending jobs", "Drained by the service between requests"],
  rendered_artifacts: ["Retained briefings", "Oldest evicted first at the cap"],
  rendered_bytes: ["Retained bytes", "Artifact bodies held for replay"],
  operation_intents: ["Open operations", "Released when an operation resolves"],
  processing_usage: ["Usage attempts", "Pruned after 30 days"],
};
interface ReceiptDiagnostics {
  status: string;
  usage: {
    output_tokens?: number;
    max_output_tokens?: number;
    counting?: string;
  };
  selected?: Array<{ reference: string; authority: string; reason: string }>;
  omissions?: string[];
}
function renderComparison(
  before: ReceiptDiagnostics,
  after: ReceiptDiagnostics,
  beforeId: string,
  afterId: string,
): HTMLElement {
  // What changed between two briefings: references added, dropped or kept,
  // status, and token cost. Diagnostics carry references, not content.
  const box = el("div", "", "comparison");
  box.append(el("h4", "Compared with the previous briefing"));
  box.append(el("p", `${beforeId} → ${afterId}`, "caption"));
  const refs = (d: ReceiptDiagnostics) =>
    new Map((d.selected ?? []).map((s) => [s.reference, s]));
  const was = refs(before),
    now = refs(after);
  const added = [...now.keys()].filter((r) => !was.has(r));
  const dropped = [...was.keys()].filter((r) => !now.has(r));
  const kept = [...now.keys()].filter((r) => was.has(r));
  const facts = el("ul");
  facts.append(
    el("li", `Status ${label(before.status)} → ${label(after.status)}`),
    el(
      "li",
      `Tokens ${String(before.usage?.output_tokens ?? "?")} → ${String(after.usage?.output_tokens ?? "?")} (budget ${String(after.usage?.max_output_tokens ?? "?")})`,
    ),
    el(
      "li",
      `${kept.length} references kept · ${added.length} added · ${dropped.length} dropped`,
    ),
  );
  box.append(facts);
  for (const [title, list, source] of [
    ["Added", added, now],
    ["Dropped", dropped, was],
  ] as const) {
    if (!list.length) continue;
    box.append(el("h5", title));
    const ul = el("ul");
    for (const reference of list) {
      const entry = source.get(reference);
      ul.append(
        el(
          "li",
          `${reference} · ${label(entry?.authority ?? "")} · ${label(entry?.reason ?? "")}`,
        ),
      );
    }
    box.append(ul);
  }
  const beforeOmissions = (before.omissions ?? []).join(", ") || "none";
  const afterOmissions = (after.omissions ?? []).join(", ") || "none";
  if (beforeOmissions !== afterOmissions)
    box.append(
      el("p", `Omissions ${beforeOmissions} → ${afterOmissions}`, "caption"),
    );
  return box;
}
const PROJECT_KEY = "memoryking.project";
let projectsLoadedFor = -1;
function rememberedProject(): string {
  const linked = /^#project=([A-Za-z0-9_%\-]{1,96})$/.exec(location.hash);
  if (linked) return decodeURIComponent(linked[1]);
  try {
    return localStorage.getItem(PROJECT_KEY) ?? "";
  } catch {
    return "";
  }
}
function rememberProject(project: string): void {
  try {
    localStorage.setItem(PROJECT_KEY, project);
  } catch {
    /* per-viewer convenience only */
  }
}
async function loadProjects(): Promise<void> {
  if (!snapshot || projectsLoadedFor === generation) return;
  projectsLoadedFor = generation;
  const context = generation;
  let items: Array<{
    project: string;
    observations: number;
    claims: number;
    tasks: number;
    receipts: number;
  }> = [];
  try {
    items = (await api("/api/projects")).items as typeof items;
  } catch {
    projectsLoadedFor = -1;
    return;
  }
  if (!snapshot || context !== generation) return;
  const remembered = rememberedProject();
  renderProjects(items, remembered);
  for (const id of ["execution-project", "receipt-project", "team-project"]) {
    const select = get<HTMLSelectElement>(id);
    const current = select.value || remembered;
    select.replaceChildren();
    if (!items.length) {
      const empty = el("option", "No readable projects yet");
      empty.value = "";
      empty.disabled = true;
      empty.selected = true;
      select.append(empty);
      continue;
    }
    for (const item of items) {
      const option = el(
        "option",
        `${item.project} · ${item.observations} memories · ${item.tasks} tasks · ${item.receipts} receipts`,
      );
      option.value = item.project;
      select.append(option);
    }
    select.value = items.some((i) => i.project === current)
      ? current
      : items[0].project;
  }
}
/** One card per readable project; opening one selects it in every view. */
function renderProjects(
  items: Array<{
    project: string;
    observations: number;
    claims: number;
    tasks: number;
    receipts: number;
    last_activity_ms?: number | null;
  }>,
  current: string,
): void {
  const grid = get("project-grid");
  grid.replaceChildren();
  get("projects-state").textContent = items.length
    ? `${items.length} readable`
    : "None readable";
  for (const item of items) {
    const card = el(
      "button",
      "",
      "project-card" + (item.project === current ? " selected" : ""),
    );
    card.type = "button";
    card.append(
      el("strong", item.project),
      el(
        "small",
        `${item.observations} memories · ${item.claims} claims · ${item.tasks} tasks · ${item.receipts} receipts`,
      ),
      el(
        "small",
        item.last_activity_ms
          ? "last activity " + new Date(item.last_activity_ms).toLocaleString()
          : "no recorded activity",
      ),
    );
    card.onclick = () => openProject(item.project);
    grid.append(card);
  }
}
/** Follow a project in every view, from a card or a #project= link. */
function openProject(project: string): void {
  rememberProject(project);
  for (const id of ["execution-project", "receipt-project", "team-project"]) {
    const select = get<HTMLSelectElement>(id);
    if (Array.from(select.options).some((o) => o.value === project))
      select.value = project;
  }
  for (const card of get("project-grid").querySelectorAll(".project-card"))
    card.classList.toggle(
      "selected",
      (card.querySelector("strong")?.textContent ?? "") === project,
    );
  history.replaceState(null, "", "#project=" + encodeURIComponent(project));
  get<HTMLFormElement>("execution-form").requestSubmit();
  get("execution-panel").scrollIntoView();
}
function renderCapacity(capacity: Snapshot["capacity"] | undefined): void {
  const list = get("capacity-list");
  list.replaceChildren();
  const entries = Object.entries(capacity ?? {});
  const warnings = entries.filter(([, v]) => v.warning).length;
  get("capacity-state").textContent = entries.length
    ? warnings
      ? `${warnings} near limit`
      : "Within limits"
    : "Not reported";
  for (const [name, value] of entries) {
    const [title, note] = CAPACITY_LABELS[name] ?? [name, ""];
    const item = el(
      "div",
      "",
      value.warning ? "capacity-item warning" : "capacity-item",
    );
    item.append(el("p", title, "eyebrow"));
    const shown =
      name === "rendered_bytes"
        ? `${(value.used / 1048576).toFixed(1)} / ${Math.round(value.limit / 1048576)} MiB`
        : `${value.used.toLocaleString()} / ${value.limit.toLocaleString()}`;
    item.append(el("strong", shown));
    const meter = el("div", "", "capacity-meter");
    const fill = el("span");
    fill.style.width = `${Math.min(100, (100 * value.used) / Math.max(1, value.limit))}%`;
    meter.append(fill);
    item.append(meter);
    item.append(el("p", note));
    list.append(item);
  }
}
const get = <T extends HTMLElement>(id: string): T =>
  document.getElementById(id) as T;
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text = "",
  cls = "",
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = cls;
  return node;
}
function label(value: string): string {
  return value.replaceAll("_", " ");
}
let snapshot: Snapshot | null = null;
let pendingReceipt: string | null = null;
/** Jump from a graph node to one of the receipts that used it, in Retrieval history. */
function openReceipt(project: string, receipt: string): void {
  const select = get<HTMLSelectElement>("receipt-project");
  if (!Array.from(select.options).some((o) => o.value === project)) {
    get("receipt-notice").textContent =
      "That project is not in your readable list; reload the project picker.";
    return;
  }
  select.value = project;
  rememberProject(project);
  pendingReceipt = receipt;
  get("retrieval-lab").scrollIntoView();
  get<HTMLFormElement>("receipt-form").requestSubmit();
}
const observatory = new Observatory(
  get("observatory"),
  api,
  () =>
    snapshot
      ? {
          csrf: snapshot.csrf,
          tenant: snapshot.tenant,
          principal: snapshot.principal,
          incarnation: snapshot.incarnation,
          authorization_epoch: snapshot.authorization_epoch,
        }
      : null,
  openReceipt,
);
const team = new Team(get("team-output"), {
  api,
  csrf: () => snapshot?.csrf ?? null,
  signedOut: (message) => {
    clear(message);
  },
});
get("team-nav").onclick = () => get("team").scrollIntoView();
get("team-project").addEventListener("change", () => {
  rememberProject(get<HTMLSelectElement>("team-project").value);
  team.clear();
});
get<HTMLFormElement>("team-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!snapshot) return;
  void team.load(get<HTMLSelectElement>("team-project").value);
});
installEmblem(get<HTMLButtonElement>("emblem-reveal"));
get("atlas-nav").onclick = () => get("observatory").scrollIntoView();
let selected = "P05";
let paused = false;
let busy = false;
let loggedOut = false;
let generation = 0;
let receiptGeneration = 0;
let receiptExpires = 0;
let liveStream: EventSource | null = null;
let streamFallback = false;
let streamSequence = 0;
function stopStream(): void {
  liveStream?.close();
  liveStream = null;
  streamSequence = 0;
}
function startStream(): void {
  if (
    !snapshot?.streaming ||
    liveStream ||
    streamFallback ||
    paused ||
    loggedOut ||
    document.hidden
  )
    return;
  const ticket = generation;
  const source = new EventSource("/api/events");
  liveStream = source;
  const reset = (): void => {
    if (ticket !== generation || source !== liveStream) return;
    generation++;
    streamFallback = true;
    clear("Live connection changed. Checking current permission with polling.");
    if (!loggedOut && !document.hidden) void refresh();
  };
  for (const kind of ["snapshot", "resync"]) {
    source.addEventListener(kind, (event) => {
      if (ticket !== generation || source !== liveStream || paused || !snapshot)
        return;
      try {
        const value = JSON.parse((event as MessageEvent).data) as Snapshot;
        if (
          value.schema_version !== 1 ||
          ![0, 12].includes(value.phases.length) ||
          value.tenant !== snapshot.tenant ||
          value.principal !== snapshot.principal ||
          value.incarnation !== snapshot.incarnation ||
          value.authorization_epoch !== snapshot.authorization_epoch
        ) {
          reset();
          return;
        }
        if (kind === "resync") {
          observatory.clear();
          clearReceipts();
          clearExecution(
            "Rebuilding the current authorized snapshot after a stream gap.",
          );
          streamSequence = 0;
        }
        const sequence = value.stream_sequence ?? 0;
        if (sequence <= streamSequence) return;
        streamSequence = sequence;
        snapshot = value;
        render();
        void refreshExecution();
      } catch {
        reset();
      }
    });
  }
  source.addEventListener("heartbeat", (event) => {
    if (ticket !== generation || source !== liveStream || paused || !snapshot)
      return;
    try {
      const value = JSON.parse((event as MessageEvent).data) as {
        fresh_until_ms: number;
      };
      snapshot.fresh_until_ms = value.fresh_until_ms;
    } catch {
      reset();
    }
  });
  source.addEventListener("context-reset", reset);
  source.onerror = reset;
}
function clearReceipts(): void {
  receiptGeneration++;
  receiptExpires = 0;
  get("receipt-list").replaceChildren();
  get("receipt-detail").replaceChildren();
}
function clear(message: string): void {
  observatory.clear();
  team.clear();
  get<HTMLSelectElement>("team-project").selectedIndex = -1;
  get("project-grid").replaceChildren();
  stopStream();
  clearExecution();
  get<HTMLSelectElement>("execution-project").selectedIndex = -1;
  clearReceipts();
  get<HTMLSelectElement>("receipt-project").selectedIndex = -1;
  get("receipt-notice").textContent = "Sign in to inspect retained receipts.";
  snapshot = null;
  get("content").hidden = true;
  get("phases").replaceChildren();
  get("detail").replaceChildren();
  get("metrics").replaceChildren();
  get("phase-map").replaceChildren();
  get("adrs").replaceChildren();
  get("provenance").textContent = "";
  get("notice").textContent = message;
  get("login").hidden = false;
  for (const id of ["pause", "refresh", "logout"]) get(id).hidden = true;
  get("freshness").textContent = "Session required";
}
async function api(
  path: string,
  data?: unknown,
): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    signal: AbortSignal.timeout(7000),
  });
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.error === "string" ? value.error : "Request failed",
    );
  return value;
}
function showDetail(): void {
  if (!snapshot) return;
  const p =
    snapshot.phases.find((p) => p.id === selected) ?? snapshot.phases[0]!;
  const box = get("detail");
  box.replaceChildren(
    el("div", p.id + " / PHASE INSPECTOR", "eyebrow"),
    el("h2", p.title),
    el("p", p.outcome),
  );
  const statuses = el("div", "", "status-grid");
  for (const [title, value] of [
    ["Execution", label(p.execution)],
    ["Verification", label(p.evidence.verification)],
    ["Human review", p.approval],
    ["Report QA", label(p.evidence.report)],
    ["Email submission", label(p.evidence.smtp)],
    ["Kindle device", p.evidence.device],
  ]) {
    const cell = el("div", "", "status-cell");
    cell.append(el("small", title.toUpperCase()), el("strong", value));
    statuses.append(cell);
  }
  box.append(statuses);
  if (p.evidence.checks?.length) {
    const checks = el("details");
    checks.append(
      el("summary", "Verification jobs at the recorded code revision"),
    );
    for (const check of p.evidence.checks)
      checks.append(
        el(
          "p",
          `${label(check.name)} · ${check.exit_code === 0 ? "passed" : "failed"} · ${check.duration_ms}ms`,
        ),
      );
    checks.append(
      el(
        "p",
        "A passing job does not establish every acceptance scenario or real-task efficacy. Read the acceptance matrix for the actual qualification scope.",
        "caption",
      ),
    );
    box.append(checks);
  }
  const source = el("p", p.approval_basis, "integrity");
  box.append(source, el("h3", "Acceptance scope"));
  const list = el("ul");
  for (const item of p.criteria) list.append(el("li", item));
  box.append(
    list,
    el(
      "p",
      "Criterion-level execution is not instrumented in this adapter. Completed reports provide the frozen verification record; phase state alone does not mark these criteria passed.",
      "caption",
    ),
  );
  const actions = el("div", "", "actions");
  for (const url of p.evidence.links) {
    const a = el(
      "a",
      url.endsWith("-pdf")
        ? "Read phase PDF"
        : url.endsWith("-epub")
          ? "Kindle edition"
          : label(url.split("-").slice(1).join("-")),
    );
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    actions.append(a);
  }
  const roadmap = el("a", "Full roadmap");
  roadmap.href = "/api/artifact/roadmap";
  roadmap.target = "_blank";
  roadmap.rel = "noopener";
  actions.append(roadmap);
  box.append(actions);
  if (p.evidence.code_revision)
    box.append(
      el(
        "p",
        "Verified code: " +
          p.evidence.code_revision +
          " · " +
          p.evidence.platform,
        "caption",
      ),
    );
  if (p.evidence.report === "invalid_or_missing")
    box.append(
      el(
        "p",
        "Completion evidence is missing or changed. This phase is excluded from verified closeout totals.",
        "integrity",
      ),
    );
  for (const node of document.querySelectorAll<HTMLElement>("[data-phase]")) {
    const active = node.dataset.phase === p.id;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  }
}
function render(): void {
  if (!snapshot) return;
  const focused = document.activeElement as HTMLElement | null;
  const focusParent = focused?.parentElement?.id;
  const focusPhase = focused?.dataset.phase;
  const focusHref = focused instanceof HTMLAnchorElement ? focused.href : null;
  get("login").hidden = true;
  get("content").hidden = false;
  get("notice").textContent = "";
  if (
    get("receipt-notice").textContent ===
    "Sign in to inspect retained receipts."
  )
    get("receipt-notice").textContent =
      "Choose a project to inspect retained receipts. No model usage is inferred.";
  for (const id of ["pause", "refresh", "logout"]) get(id).hidden = false;
  get("freshness").textContent = paused
    ? "Updates paused · snapshot ages normally"
    : snapshot.streaming && !streamFallback
      ? "Current snapshot · live updates and 5s polling"
      : "Current snapshot · polled every 5s";
  renderCapacity(snapshot.capacity);
  void loadProjects();
  get("development-record").hidden = snapshot.phases.length === 0;
  get("record-nav").hidden = snapshot.phases.length === 0;
  if (!snapshot.phases.length) {
    for (const id of ["phases", "detail", "metrics", "phase-map", "adrs"])
      get(id).replaceChildren();
    get("provenance").textContent =
      `${snapshot.tenant} / ${snapshot.principal} · history ${snapshot.incarnation} · observed ${new Date(snapshot.observed_ms).toLocaleString()}`;
    return;
  }
  get("baseline").textContent = "Plan " + snapshot.plan_revision;
  const metrics = get("metrics");
  metrics.replaceChildren();
  const pending = snapshot.phases.filter(
    (p) =>
      ["complete", "partial", "blocked"].includes(p.execution) &&
      p.evidence.report === "qa_passed" &&
      p.approval !== "Report approved",
  ).length;
  for (const [title, value, note] of [
    [
      "TECHNICALLY COMPLETE",
      `${snapshot.counts.closed} / ${snapshot.counts.total}`,
      "Complete phases with intact reports; equal phase counts",
    ],
    [
      "REPORT CLOSEOUTS",
      `${snapshot.counts.report_closeouts} / ${snapshot.counts.total}`,
      "QA accepted, including partial or blocked outcomes",
    ],
    [
      "LOCAL VERIFICATION",
      String(snapshot.counts.historical_jobs_passed),
      "Passing jobs at recorded code revisions",
    ],
    [
      "REVIEW QUEUE",
      String(pending),
      "Submitted phase reports awaiting review",
    ],
    [
      "TASK TELEMETRY",
      "Project view",
      "Connect a project in Task activity below",
    ],
  ]) {
    const card = el("article", "", "card metric");
    card.append(
      el("div", title, "eyebrow"),
      el("strong", value),
      el("p", note),
    );
    metrics.append(card);
  }
  get("phases").replaceChildren();
  get("phase-map").replaceChildren();
  for (const p of snapshot.phases) {
    const button = el("button", "", "phase-row " + p.execution);
    button.dataset.phase = p.id;
    const text = el("div");
    text.append(el("h3", p.title), el("small", label(p.execution)));
    button.append(el("span", p.id, "phase-number"), text);
    button.addEventListener("click", () => {
      selected = p.id;
      showDetail();
    });
    get("phases").append(button);
    const node = el("button", "", "phase-node " + p.execution);
    node.dataset.phase = p.id;
    node.append(
      el("strong", p.id),
      el(
        "span",
        p.execution === "complete"
          ? "Closed"
          : p.execution === "in_progress"
            ? "Active"
            : p.execution === "partial"
              ? "Partial"
              : p.execution === "blocked"
                ? "Blocked"
                : p.execution === "closeout_pending"
                  ? "Reporting"
                  : "—",
      ),
    );
    node.title = p.title;
    node.setAttribute(
      "aria-label",
      p.id + " " + p.title + " " + label(p.execution),
    );
    node.addEventListener("click", () => {
      selected = p.id;
      showDetail();
    });
    get("phase-map").append(node);
  }
  get("adrs").replaceChildren();
  for (const adr of snapshot.adrs) {
    const a = el("a", "ADR " + adr.id.slice(0, 3));
    a.href = adr.url;
    a.title = adr.id;
    a.target = "_blank";
    a.rel = "noopener";
    get("adrs").append(a);
  }
  get("provenance").textContent =
    `${snapshot.adapter} · ${snapshot.tenant} / ${snapshot.principal} · ${snapshot.incarnation} · observed ${new Date(snapshot.observed_ms).toLocaleString()} · plan SHA-256 ${snapshot.source_hash}`;
  showDetail();
  // Polling replaces nodes, so restore the user's existing keyboard position.
  if (focusPhase && (focusParent === "phases" || focusParent === "phase-map")) {
    for (const node of get(focusParent).querySelectorAll<HTMLElement>(
      "[data-phase]",
    )) {
      if (node.dataset.phase === focusPhase)
        node.focus({ preventScroll: true });
    }
  } else if (focusHref) {
    for (const node of get("content").querySelectorAll<HTMLAnchorElement>(
      "a",
    )) {
      if (node.href === focusHref) {
        node.focus({ preventScroll: true });
        break;
      }
    }
  }
}
async function refresh(): Promise<void> {
  if (busy || loggedOut) return;
  busy = true;
  const ticket = generation;
  try {
    const value = (await api("/api/snapshot")) as unknown as Snapshot;
    if (ticket !== generation) return;
    if (value.schema_version !== 1 || ![0, 12].includes(value.phases.length))
      throw new Error("Snapshot unavailable");
    if (
      snapshot &&
      (snapshot.incarnation !== value.incarnation ||
        snapshot.principal !== value.principal ||
        snapshot.tenant !== value.tenant ||
        snapshot.authorization_epoch !== value.authorization_epoch)
    ) {
      clear("Authorization context changed.");
    }
    snapshot = value;
    render();
    startStream();
    void refreshExecution();
  } catch (error) {
    if (ticket === generation)
      clear(
        "Session or source unavailable. " +
          (error instanceof Error ? label(error.message) : ""),
      );
  } finally {
    busy = false;
  }
}
get<HTMLFormElement>("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  generation++;
  loggedOut = false;
  stopStream();
  streamFallback = false;
  try {
    await api("/api/bootstrap", { code: get<HTMLInputElement>("code").value });
    get<HTMLInputElement>("code").value = "";
    await refresh();
  } catch (error) {
    clear(error instanceof Error ? label(error.message) : "Sign-in failed");
  }
});
get("logout").addEventListener("click", async () => {
  const csrf = snapshot?.csrf;
  generation++;
  loggedOut = true;
  clear("Signed out.");
  try {
    await api("/api/logout", { csrf });
  } catch {
    get("notice").textContent =
      "Local view cleared. Session termination was not confirmed; it expires within 15 minutes.";
  }
});
get("refresh").addEventListener("click", () => void refresh());
// `memoryking dashboard open` hands the one-use code over in the URL fragment, which
// never reaches the server; it is consumed and removed from the address bar at once.
const fragmentCode = /^#code=([A-Za-z0-9_\\-]{16,128})$/.exec(location.hash);
if (fragmentCode) {
  history.replaceState(null, "", location.pathname);
  get<HTMLInputElement>("code").value = fragmentCode[1];
  get<HTMLFormElement>("login-form").requestSubmit();
}
get("overview").addEventListener("click", () => window.scrollTo({ top: 0 }));
get("retrieval-nav").addEventListener("click", () =>
  get("retrieval-lab").scrollIntoView(),
);
get("receipt-project").addEventListener("change", () => {
  rememberProject(get<HTMLSelectElement>("receipt-project").value);
  clearReceipts();
});
get<HTMLFormElement>("receipt-form").addEventListener(
  "submit",
  async (event) => {
    event.preventDefault();
    clearReceipts();
    if (!snapshot) return;
    const ticket = receiptGeneration;
    const context = generation;
    const project = get<HTMLSelectElement>("receipt-project").value;
    get("receipt-notice").textContent = "Checking current access…";
    try {
      const result = await api("/api/receipts/" + encodeURIComponent(project));
      if (!snapshot || ticket !== receiptGeneration || context !== generation)
        return;
      receiptExpires = Date.now() + 15000;
      const items = result.items as Array<{
        receipt: string;
        status: string;
        created_ms: number;
        usage: { output_tokens: number; counting: string };
        cache: string;
      }>;
      get("receipt-notice").textContent = items.length
        ? `${items.length} retained receipts · snapshot clears after 15 seconds`
        : "No currently available receipts for this owner and project.";
      for (const item of items) {
        const button = el("button", "", "receipt-row");
        button.dataset.receipt = item.receipt;
        button.append(
          el("strong", label(item.status)),
          el(
            "small",
            `${item.usage.output_tokens ?? "Unknown"} tokens · ${item.usage.counting ?? "not counted"} · cache ${item.cache}`,
          ),
          el("small", new Date(item.created_ms).toLocaleString()),
        );
        button.addEventListener("click", async () => {
          try {
            const detail = await api(
              "/api/receipt/" +
                encodeURIComponent(project) +
                "/" +
                encodeURIComponent(item.receipt),
            );
            if (
              !snapshot ||
              ticket !== receiptGeneration ||
              context !== generation
            )
              return;
            receiptExpires = Date.now() + 15000;
            const diag = detail.diagnostics as {
              status: string;
              usage: {
                output_tokens: number;
                max_output_tokens: number;
                serialized_bytes: number;
                counting: string;
                provider_usage: string;
              };
              cache?: string;
              minimum_required_tokens?: number | null;
              coverage?: { lexical: string; dense: string; optional: string };
              source_position?: number;
              trace?: Array<Record<string, unknown>>;
              selected?: Array<{
                reference: string;
                authority: string;
                reason: string;
              }>;
              omissions?: string[];
            };
            const panel = get("receipt-detail");
            const meter = el("meter");
            meter.max = diag.usage.max_output_tokens || 1;
            meter.value = diag.usage.output_tokens || 0;
            meter.hidden = typeof diag.usage.output_tokens !== "number";
            if (diag.status === "budget_insufficient")
              meter.className = "budget-failed";
            meter.setAttribute("aria-label", "Serialized token budget used");
            panel.replaceChildren(
              el("div", "Response details", "eyebrow"),
              el("h3", label(diag.status)),
              el(
                "p",
                `${diag.usage.output_tokens ?? "Unknown"} / ${diag.usage.max_output_tokens ?? "not applicable"} tokens · ${diag.usage.counting ?? "not counted for this artifact"}`,
              ),
              meter,
              el(
                "p",
                `${diag.usage.serialized_bytes} serialized bytes · Provider usage: ${diag.usage.provider_usage}`,
              ),
            );
            if (diag.status === "budget_insufficient") {
              panel.prepend(
                el(
                  "p",
                  `No briefing fits. Minimum essential budget: ${diag.minimum_required_tokens ?? "not computable"} tokens. The displayed count measures only the failure response.`,
                  "integrity",
                ),
              );
            }
            if (diag.coverage)
              panel.append(
                el(
                  "p",
                  `Channels: lexical ${diag.coverage.lexical}; dense ${diag.coverage.dense}. Optional coverage: ${label(diag.coverage.optional)}.`,
                  "caption",
                ),
              );
            for (const stage of diag.trace ?? []) {
              const row = el("div", "", "trace-stage");
              row.append(el("strong", label(String(stage.stage))));
              const facts = el("div", "", "trace-facts");
              for (const [key, value] of Object.entries(stage)) {
                if (key === "stage") continue;
                facts.append(
                  el(
                    "span",
                    key === "duration_ms"
                      ? `Duration: ${value} ms`
                      : `${label(key)}: ${typeof value === "string" ? label(value) : String(value)}`,
                  ),
                );
              }
              row.append(facts);
              panel.append(row);
            }
            const list = el("ul");
            for (const selected of diag.selected ?? [])
              list.append(
                el(
                  "li",
                  `${label(selected.authority)} · ${label(selected.reason)} · ${selected.reference}`,
                ),
              );
            panel.append(
              list,
              el(
                "p",
                `Omissions: ${(diag.omissions ?? []).join(", ") || "none recorded"}`,
                "caption",
              ),
              el("code", "SHA-256 " + String(detail.rendered_hash)),
              el(
                "p",
                "Inspecting this receipt does not execute anything. Exact replay rechecks permission on retained bytes; rerun compiles a new artifact from explicit inputs. Neither calls a model.",
                "caption",
              ),
            );
            const compare = el("button", "Compare with previous briefing");
            compare.type = "button";
            compare.addEventListener("click", async () => {
              // The list is newest first, so the previous briefing is the next entry.
              const previous = items[items.indexOf(item) + 1];
              if (!previous) {
                compare.replaceWith(
                  el("p", "No earlier receipt to compare with.", "caption"),
                );
                return;
              }
              try {
                const before = await api(
                  "/api/receipt/" +
                    encodeURIComponent(project) +
                    "/" +
                    encodeURIComponent(previous.receipt),
                );
                if (
                  !snapshot ||
                  ticket !== receiptGeneration ||
                  context !== generation
                )
                  return;
                compare.replaceWith(
                  renderComparison(
                    before.diagnostics as ReceiptDiagnostics,
                    diag as ReceiptDiagnostics,
                    previous.receipt,
                    item.receipt,
                  ),
                );
              } catch (error) {
                compare.replaceWith(
                  el(
                    "p",
                    "Comparison unavailable: " +
                      (error instanceof Error
                        ? label(error.message)
                        : "request failed"),
                    "caption",
                  ),
                );
              }
            });
            panel.append(compare);
            const replay = el("button", "Exact retained replay"),
              rerun = el("button", "Rerun retrieval with these inputs"),
              inputs = el("textarea"),
              output = el("pre");
            inputs.setAttribute("aria-label", "New briefing inputs JSON");
            inputs.rows = 8;
            inputs.value = JSON.stringify(
              {
                project,
                query: "inspect current evidence",
                bindings: [{}],
                max_output_tokens: 6000,
                tokenizer: "cl100k_base",
              },
              null,
              2,
            );
            const invoke = async (
              action: "replay" | "rerun",
            ): Promise<void> => {
              if (
                !snapshot ||
                ticket !== receiptGeneration ||
                context !== generation
              )
                return;
              replay.disabled = rerun.disabled = true;
              try {
                const result = await api("/api/command", {
                  csrf: snapshot.csrf,
                  operation: "retrieval",
                  command: {
                    version: 1,
                    incarnation: snapshot.incarnation,
                    action,
                    args:
                      action === "replay"
                        ? { project, receipt: item.receipt }
                        : {
                            project,
                            predecessor: item.receipt,
                            brief: JSON.parse(inputs.value),
                          },
                  },
                });
                if (
                  !snapshot ||
                  ticket !== receiptGeneration ||
                  context !== generation
                )
                  return;
                output.textContent =
                  (action === "replay"
                    ? "Exact retained artifact (displayed as formatted JSON)."
                    : "New retrieval artifact linked to the selected predecessor; results may differ.") +
                  "\n" +
                  JSON.stringify(result, null, 2);
                // Never extend the displayed snapshot's original lifetime.
              } catch (error) {
                if (ticket !== receiptGeneration || context !== generation)
                  return;
                output.textContent = `Unavailable: ${error instanceof Error ? error.message : "request failed"}. Reload before another attempt.`;
              } finally {
                replay.disabled = rerun.disabled = false;
              }
            };
            replay.onclick = () => void invoke("replay");
            rerun.onclick = () => void invoke("rerun");
            const rerunBox = el("details");
            rerunBox.append(
              el("summary", "New retrieval inputs (no model call)"),
              inputs,
              rerun,
            );
            panel.append(replay, rerunBox, output);
          } catch {
            clearReceipts();
            get("receipt-notice").textContent =
              "Receipt unavailable under current permissions. Reload to check again.";
          }
        });
        get("receipt-list").append(button);
      }
      if (pendingReceipt) {
        const wanted = pendingReceipt;
        pendingReceipt = null;
        const row = get("receipt-list").querySelector<HTMLButtonElement>(
          `[data-receipt="${CSS.escape(wanted)}"]`,
        );
        if (row) row.click();
        else
          get("receipt-notice").textContent +=
            " · the cited receipt is no longer retained for this account";
      }
    } catch {
      if (ticket !== receiptGeneration || context !== generation) return;
      clearReceipts();
      get("receipt-notice").textContent =
        "Receipts unavailable. Check the project and current session.";
    }
  },
);
get("pause").addEventListener("click", () => {
  paused = !paused;
  get("pause").textContent = paused ? "Resume updates" : "Pause updates";
  get("freshness").textContent = paused
    ? "Updates paused · snapshot ages normally"
    : "Refreshing";
  if (paused) stopStream();
  if (!paused) void refresh();
});
get("theme").addEventListener("click", () =>
  document.body.classList.toggle("light"),
);
setInterval(() => {
  observatory.tick();
  if (executionExpires && Date.now() >= executionExpires) {
    clearExecution(
      "Task snapshot expired. Reconnect to check current permissions.",
    );
  }
  if (receiptExpires && Date.now() >= receiptExpires) {
    clearReceipts();
    get("receipt-notice").textContent =
      "Receipt snapshot expired. Load again to recheck permissions.";
  }
  if (snapshot && Date.now() > snapshot.fresh_until_ms) {
    clear(
      "Snapshot expired. Refresh checks current authorization before displaying content.",
    );
    get("refresh").hidden = false;
  }
}, 250);
setInterval(() => {
  if (!paused && !document.hidden) void refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    generation++;
    clear("View cleared while hidden. Reconnecting checks current permission.");
  } else if (!loggedOut) void refresh();
});
interface TaskCriterion {
  id: string;
  text: string;
  reported: string;
  weight: number;
  effective: string;
  evidence_available: boolean;
  evidence: { kind: string; id: string }[];
  decision: string | null;
  definition_changed: boolean;
  reverification_required: boolean;
  reopened: boolean;
}
interface PlanCounts {
  total: number;
  completed: number;
  resolved: number;
  doing: number;
  todo: number;
  blocked: number;
  skipped: number;
  unknown: number;
  work_percent: number | null;
}
interface EvidenceRef {
  kind: string;
  id: string;
}
interface PlanStep {
  id: string;
  phase: string;
  description: string;
  status: string;
  reported_status: string;
  notes: string;
  decision: string | null;
  depends_on: string[];
  evidence: EvidenceRef[];
}
interface PlanProgress extends PlanCounts {
  current_phase: string | null;
  phases: { id: string; title: string; state: string; progress: PlanCounts }[];
}
interface PlanSummary {
  id: string;
  title: string;
  objective: string;
  status: string;
  revision: number;
  updated_ms: number;
  history_complete: boolean;
  progress: PlanProgress;
}
interface PlanSnapshot {
  schema_version: number;
  project: string;
  tenant: string;
  principal: string;
  incarnation: string;
  fresh_until_ms: number;
  kind: "plan" | "plans";
  plans?: PlanSummary[];
  cursor?: string | null;
  detail?: {
    id: string;
    revision: number;
    updated_ms: number;
    history_complete: boolean;
    progress: PlanProgress;
    plan: {
      title: string;
      objective: string;
      status: string;
      context: string;
      closure_note: string;
      steps: PlanStep[];
      lessons: EvidenceRef[];
    };
  };
}
let selectedPlan = "";
let planCursor: string | null = null;
function planAction(text: string, action: () => void): HTMLButtonElement {
  const button = el("button", text);
  button.type = "button";
  button.addEventListener("click", () => {
    if (!snapshot || Date.now() >= executionExpires) return;
    executionGeneration++;
    get("execution-plans").replaceChildren(
      el("p", "Loading current plan view…"),
    );
    action();
    void refreshExecution();
  });
  return button;
}
function planMeter(p: PlanCounts, title: string): HTMLElement {
  const box = el("div", "", "structured-plan-progress");
  const meter = el("progress");
  meter.max = Math.max(1, p.total);
  meter.value = p.completed;
  meter.setAttribute(
    "aria-label",
    `${title}: ${p.completed} of ${p.total} steps completed`,
  );
  box.append(
    el(
      "strong",
      p.total
        ? `${p.work_percent}% · ${p.completed} of ${p.total} steps completed`
        : "No steps defined",
    ),
    meter,
    el(
      "p",
      `${p.doing} in progress · ${p.todo} to do · ${p.blocked} blocked · ${p.skipped} skipped · ${p.unknown} need verification`,
    ),
  );
  if (p.skipped)
    box.append(
      el(
        "p",
        `${p.resolved} resolved including skipped work. Skipped steps do not count as completed.`,
      ),
    );
  return box;
}
function renderPlans(value: PlanSnapshot): void {
  const root = get("execution-plans");
  root.replaceChildren();
  const detail = value.detail;
  const items = detail ? [{ ...detail, ...detail.plan }] : (value.plans ?? []);
  if (detail || planCursor)
    root.append(
      planAction("Back to plans", () => {
        selectedPlan = "";
        planCursor = null;
      }),
    );
  if (!items.length) root.append(el("p", "No structured plans on this page."));
  for (const p of items) {
    const card = el("article", "", "task-card structured-plan");
    card.append(
      el("h3", p.title),
      el("p", p.objective),
      el(
        "p",
        `${p.status} · Revision ${p.revision} · Updated ${new Date(p.updated_ms).toLocaleString()}`,
        "eyebrow",
      ),
      planMeter(p.progress, p.title),
    );
    if (!p.history_complete)
      card.append(
        el(
          "p",
          "Incomplete history: affected completion requires verification.",
        ),
      );
    if (!detail)
      card.append(
        planAction("View plan", () => {
          selectedPlan = p.id;
          planCursor = null;
        }),
      );
    else {
      if (detail.plan.context) card.append(el("p", detail.plan.context));
      for (const phase of p.progress.phases) {
        const box = el("details", "", "plan-phase");
        box.dataset.task = `plan-${p.id}-${phase.id}`;
        const summary = el(
          "summary",
          `${phase.id} · ${phase.title} · ${phase.progress.completed}/${phase.progress.total} completed`,
        );
        box.append(summary, planMeter(phase.progress, phase.title));
        for (const step of detail.plan.steps.filter(
          (s) => s.phase === phase.id,
        )) {
          const row = el("section", "", "plan-step");
          row.append(
            el("h4", step.description),
            el("p", `${step.id} · ${step.status}`),
          );
          if (step.depends_on.length)
            row.append(el("p", `Depends on: ${step.depends_on.join(", ")}`));
          if (step.notes) row.append(el("p", step.notes));
          if (step.decision)
            row.append(el("p", `Scope decision: ${step.decision}`));
          if (step.reported_status !== step.status)
            row.append(
              el(
                "p",
                `Reported ${step.reported_status}; current evidence requires verification.`,
              ),
            );
          for (const e of step.evidence)
            row.append(el("code", `${e.kind}: ${e.id}`, "source-evidence"));
          box.append(row);
        }
        card.append(box);
      }
      if (detail.plan.closure_note)
        card.append(el("h4", "Closure"), el("p", detail.plan.closure_note));
      for (const e of detail.plan.lessons)
        card.append(el("code", `Lesson ${e.kind}: ${e.id}`, "source-evidence"));
    }
    root.append(card);
  }
  if (value.cursor)
    root.append(
      planAction("More plans", () => {
        planCursor = value.cursor ?? null;
      }),
    );
}
interface ExecutionTask {
  plan_id?: string;
  task: string;
  run: string | null;
  checkpoint: string;
  revision: number;
  writer: string;
  source_position: number;
  body_hash: string;
  objective: string;
  current_state: string;
  summary: {
    text: string;
    method: string;
    author: string;
    created_ms: number;
  } | null;
  criteria: TaskCriterion[];
  progress: {
    passed: number;
    failed: number;
    pending: number;
    waived: number;
    unknown: number;
    total: number;
    scope_hash: string;
    history_complete: boolean;
    weighted: {
      passed: number;
      total: number;
      waived: number;
      unknown: number;
    };
    previous_total: number | null;
    scope_changed: boolean | null;
    reopened: number;
  };
  blockers: { text: string; owner: string | null }[];
  operations: { id: string; state: string; reconciliation_required: boolean }[];
  verification: {
    name: string;
    result: string;
    target_revision: string | null;
    environment: string;
  }[];
  capture_gaps: string[];
  heartbeat: { status: string; recorded_ms: number | null; activity?: string };
}
interface ExecutionSnapshot {
  schema_version: number;
  project: string;
  tenant: string;
  principal: string;
  incarnation: string;
  observed_ms: number;
  fresh_until_ms: number;
  status: string;
  reason?: string;
  tasks: ExecutionTask[];
  events: {
    id: string;
    kind: string;
    task: string;
    run: string | null;
    actor: string;
    recorded_ms: number;
    observed_ms: number;
    source_position: number;
  }[];
}
let executionProject = "";
let executionGeneration = 0;
let executionExpires = 0;
let executionBusy = false;
function clearExecution(message = "Enter a project to load its tasks."): void {
  executionGeneration++;
  executionExpires = 0;
  executionProject = "";
  get("execution-tasks").replaceChildren();
  get("execution-plans").replaceChildren();
  selectedPlan = "";
  planCursor = null;
  get("execution-events").replaceChildren();
  get("execution-provenance").textContent = "";
  get("execution-notice").textContent = message;
}
function renderExecution(value: ExecutionSnapshot): void {
  const tasks = get("execution-tasks");
  tasks.replaceChildren();
  get("execution-notice").textContent = value.tasks.length
    ? `${value.tasks.length} task record${value.tasks.length === 1 ? "" : "s"} · refreshed every 5s while updates run`
    : "No tasks in the current project view.";
  for (const task of value.tasks) {
    if (task.plan_id) continue;
    const card = el("article", "", "task-card");
    card.append(
      el(
        "div",
        `Saved by ${task.writer} · Task ${task.task}${task.run ? ` · Run ${task.run}` : ""}`,
        "eyebrow",
      ),
      el("h3", task.objective),
    );
    if (task.summary) {
      card.append(
        el("p", "Where this left off", "eyebrow"),
        el("p", task.summary.text, "task-summary"),
        el(
          "p",
          `Summary ${label(task.summary.method)} by ${task.summary.author} · ${new Date(task.summary.created_ms).toLocaleString()} · the checkpoint is the record`,
          "caption",
        ),
      );
    }
    card.append(el("p", task.current_state));
    const p = task.progress;
    const summary = `${p.passed} passed · ${p.pending} pending · ${p.failed} failed · ${p.unknown} unknown · ${p.waived} waived / ${p.total} criteria`;
    const bar = el("div", "", "criteria-bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", summary);
    for (const c of task.criteria) {
      const segment = el("span", "", "criterion-segment " + c.effective);
      segment.title = c.id + ": " + c.effective;
      segment.setAttribute("aria-hidden", "true");
      bar.append(segment);
    }
    card.append(
      bar,
      el("p", summary, "criterion-summary"),
      el(
        "p",
        `Weighted: ${p.weighted.passed} passed / ${p.weighted.total} declared units; ${p.weighted.waived} waived, ${p.weighted.unknown} unknown. Missing weights default to 1.`,
        "caption",
      ),
    );
    if (p.scope_changed)
      card.append(
        el(
          "p",
          `Scope changed since previous checkpoint: ${p.previous_total} → ${p.total} criteria.`,
          "integrity",
        ),
      );
    if (p.reopened)
      card.append(
        el(
          "p",
          `${p.reopened} previously passed criteria reopened since previous checkpoint.`,
          "integrity",
        ),
      );
    if (!p.history_complete)
      card.append(
        el(
          "p",
          "Definition history is incomplete; passed claims remain unknown.",
          "integrity",
        ),
      );
    const heartbeat = task.heartbeat;
    card.append(
      el(
        "p",
        heartbeat.status === "recent"
          ? `Heartbeat observed ${new Date(heartbeat.recorded_ms!).toLocaleTimeString()} · reported ${label(heartbeat.activity ?? "unknown")}`
          : `Heartbeat ${heartbeat.status} · current liveness unknown`,
        "heartbeat " + heartbeat.status,
      ),
    );
    if (task.blockers.length) {
      const blockers = el("ul", "", "blocker-list");
      for (const b of task.blockers)
        blockers.append(
          el("li", `${b.text} · owner: ${b.owner ?? "unassigned"}`),
        );
      card.append(el("h4", "Reported blockers"), blockers);
    }
    const details = el("details");
    details.dataset.task = task.task + "/" + (task.run ?? "task");
    details.append(el("summary", "Inspect criteria, evidence and source"));
    for (const c of task.criteria) {
      const row = el("div", "", "criterion-row");
      row.append(el("strong", `${c.id} · ${c.effective}`), el("p", c.text));
      if (c.reported !== c.effective)
        row.append(
          el(
            "p",
            `Producer reported ${c.reported}; current evidence or definition checks prevent credit.`,
            "integrity",
          ),
        );
      if (c.reverification_required)
        row.append(
          el(
            "p",
            "Reverification required: changed definition with reused proof, or incomplete history.",
            "caption",
          ),
        );
      for (const e of c.evidence) row.append(el("code", `${e.kind}: ${e.id}`));
      if (c.decision)
        row.append(el("p", `Waiver decision: ${c.decision}`, "caption"));
      details.append(row);
    }
    for (const v of task.verification)
      details.append(
        el(
          "p",
          `${v.name}: ${label(v.result)} · ${v.environment} · revision ${v.target_revision ?? "unspecified"}`,
          "caption",
        ),
      );
    for (const op of task.operations)
      details.append(
        el(
          "p",
          `${op.id}: ${label(op.state)}${op.reconciliation_required ? " · reconciliation required" : ""}`,
          "caption",
        ),
      );
    for (const gap of task.capture_gaps)
      details.append(el("p", "Capture gap: " + gap, "integrity"));
    details.append(
      el(
        "p",
        `Checkpoint ${task.checkpoint} · revision ${task.revision} · writer ${task.writer} · position ${task.source_position}`,
        "caption",
      ),
      el("code", "Body SHA-256 " + task.body_hash),
      el("code", "Criterion scope " + p.scope_hash),
      el(
        "p",
        "Display text is bounded. Use full resume for complete task state. Repository freshness must be inspected against the current snapshot.",
        "caption",
      ),
    );
    card.append(details);
    tasks.append(card);
  }
  const events = get("execution-events");
  events.replaceChildren();
  for (const event of value.events) {
    const row = el("li");
    row.append(
      el("strong", label(event.kind)),
      el("p", `${event.task} · ${event.actor}`, "caption"),
      el(
        "p",
        `Recorded ${new Date(event.recorded_ms).toLocaleString()}`,
        "caption",
      ),
      el(
        "p",
        `Observed ${new Date(event.observed_ms).toLocaleString()} · position ${event.source_position}`,
        "caption",
      ),
    );
    events.append(row);
  }
  if (!value.events.length)
    events.append(el("li", "No linked activity records visible."));
  get("execution-provenance").textContent =
    `${value.project} · ${value.tenant} / ${value.principal} · history ${value.incarnation} · observed ${new Date(value.observed_ms).toLocaleString()}. Bounded view: up to 8 visible tasks from 64 recent streams, 32 events from 128 candidates, 128 prior checkpoints per stream. No hidden totals, private reasoning, cost or ETA.`;
}
async function refreshExecution(): Promise<void> {
  if (!snapshot || !executionProject || executionBusy) return;
  executionBusy = true;
  const ticket = executionGeneration,
    context = generation,
    project = executionProject;
  const started = Date.now();
  try {
    const value = (await api(
      "/api/execution/" + encodeURIComponent(project),
    )) as unknown as ExecutionSnapshot;
    const plans = (await api(
      "/api/plans/" +
        encodeURIComponent(project) +
        (selectedPlan
          ? "/" + encodeURIComponent(selectedPlan)
          : planCursor
            ? "?cursor=" + encodeURIComponent(planCursor)
            : ""),
    )) as unknown as PlanSnapshot;
    if (ticket !== executionGeneration || context !== generation || !snapshot)
      return;
    if (
      plans.schema_version !== 1 ||
      plans.project !== project ||
      plans.tenant !== snapshot.tenant ||
      plans.principal !== snapshot.principal ||
      plans.incarnation !== snapshot.incarnation ||
      value.schema_version !== 1 ||
      value.status !== "available" ||
      value.project !== project ||
      value.incarnation !== snapshot.incarnation ||
      value.tenant !== snapshot.tenant ||
      value.principal !== snapshot.principal ||
      Date.now() >= started + 15000
    )
      throw new Error("Current task authorization or evidence unavailable");
    executionExpires = Math.min(
      started + 15000,
      value.fresh_until_ms,
      plans.fresh_until_ms,
    );
    if (Date.now() >= executionExpires) throw new Error("Snapshot expired");
    // Preserve disclosure state and keyboard position across observational polling.
    const open = new Set(
      Array.from(
        get("execution-panel").querySelectorAll<HTMLDetailsElement>(
          "details[open]",
        ),
        (d) => d.dataset.task,
      ),
    );
    const focused = (
      document.activeElement?.closest("details") as HTMLDetailsElement | null
    )?.dataset.task;
    renderExecution(value);
    renderPlans(plans);
    for (const d of get("execution-panel").querySelectorAll<HTMLDetailsElement>(
      "details",
    )) {
      d.open = open.has(d.dataset.task);
      if (focused === d.dataset.task)
        d.querySelector("summary")?.focus({ preventScroll: true });
    }
  } catch {
    if (ticket === executionGeneration && context === generation)
      clearExecution(
        "Task data unavailable. Reconnect to recheck project access and current evidence.",
      );
  } finally {
    executionBusy = false;
  }
}
get("execution-nav").addEventListener("click", () =>
  get("execution-panel").scrollIntoView(),
);
get("record-nav").addEventListener("click", () =>
  get("development-record").scrollIntoView(),
);
get("execution-project").addEventListener("change", () => {
  rememberProject(get<HTMLSelectElement>("execution-project").value);
  clearExecution();
});
get<HTMLFormElement>("execution-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearExecution();
  if (!snapshot) return;
  executionProject = get<HTMLSelectElement>("execution-project").value;
  get("execution-notice").textContent = "Checking current task access…";
  void refreshExecution();
});
void refresh();
