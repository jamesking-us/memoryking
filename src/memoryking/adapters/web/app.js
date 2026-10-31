import { Observatory } from "./observatory.js";
import { installEmblem } from "./brand.js";
const CAPACITY_LABELS = {
    jobs: ["Pending jobs", "Drained by the service between requests"],
    rendered_artifacts: ["Retained briefings", "Oldest evicted first at the cap"],
    rendered_bytes: ["Retained bytes", "Artifact bodies held for replay"],
    operation_intents: ["Open operations", "Released when an operation resolves"],
    processing_usage: ["Usage attempts", "Pruned after 30 days"],
};
function renderCapacity(capacity) {
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
        const item = el("div", "", value.warning ? "capacity-item warning" : "capacity-item");
        item.append(el("p", title, "eyebrow"));
        const shown = name === "rendered_bytes"
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
const get = (id) => document.getElementById(id);
function el(tag, text = "", cls = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = cls;
    return node;
}
function label(value) {
    return value.replaceAll("_", " ");
}
let snapshot = null;
const observatory = new Observatory(get("observatory"), api, () => snapshot
    ? {
        csrf: snapshot.csrf,
        tenant: snapshot.tenant,
        principal: snapshot.principal,
        incarnation: snapshot.incarnation,
        authorization_epoch: snapshot.authorization_epoch,
    }
    : null);
installEmblem(get("emblem-reveal"));
get("atlas-nav").onclick = () => get("observatory").scrollIntoView();
let selected = "P05";
let paused = false;
let busy = false;
let loggedOut = false;
let generation = 0;
let receiptGeneration = 0;
let receiptExpires = 0;
let liveStream = null;
let streamFallback = false;
let streamSequence = 0;
function stopStream() {
    liveStream?.close();
    liveStream = null;
    streamSequence = 0;
}
function startStream() {
    if (!snapshot?.streaming ||
        liveStream ||
        streamFallback ||
        paused ||
        loggedOut ||
        document.hidden)
        return;
    const ticket = generation;
    const source = new EventSource("/api/events");
    liveStream = source;
    const reset = () => {
        if (ticket !== generation || source !== liveStream)
            return;
        generation++;
        streamFallback = true;
        clear("Live connection changed. Checking current permission with polling.");
        if (!loggedOut && !document.hidden)
            void refresh();
    };
    for (const kind of ["snapshot", "resync"]) {
        source.addEventListener(kind, (event) => {
            if (ticket !== generation || source !== liveStream || paused || !snapshot)
                return;
            try {
                const value = JSON.parse(event.data);
                if (value.schema_version !== 1 ||
                    ![0, 12].includes(value.phases.length) ||
                    value.tenant !== snapshot.tenant ||
                    value.principal !== snapshot.principal ||
                    value.incarnation !== snapshot.incarnation ||
                    value.authorization_epoch !== snapshot.authorization_epoch) {
                    reset();
                    return;
                }
                if (kind === "resync") {
                    observatory.clear();
                    clearReceipts();
                    clearExecution("Rebuilding the current authorized snapshot after a stream gap.");
                    streamSequence = 0;
                }
                const sequence = value.stream_sequence ?? 0;
                if (sequence <= streamSequence)
                    return;
                streamSequence = sequence;
                snapshot = value;
                render();
                void refreshExecution();
            }
            catch {
                reset();
            }
        });
    }
    source.addEventListener("heartbeat", (event) => {
        if (ticket !== generation || source !== liveStream || paused || !snapshot)
            return;
        try {
            const value = JSON.parse(event.data);
            snapshot.fresh_until_ms = value.fresh_until_ms;
        }
        catch {
            reset();
        }
    });
    source.addEventListener("context-reset", reset);
    source.onerror = reset;
}
function clearReceipts() {
    receiptGeneration++;
    receiptExpires = 0;
    get("receipt-list").replaceChildren();
    get("receipt-detail").replaceChildren();
}
function clear(message) {
    observatory.clear();
    stopStream();
    clearExecution();
    get("execution-project").value = "";
    clearReceipts();
    get("receipt-project").value = "";
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
    for (const id of ["pause", "refresh", "logout"])
        get(id).hidden = true;
    get("freshness").textContent = "Session required";
}
async function api(path, data) {
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
        throw new Error(typeof value.error === "string" ? value.error : "Request failed");
    return value;
}
function showDetail() {
    if (!snapshot)
        return;
    const p = snapshot.phases.find((p) => p.id === selected) ?? snapshot.phases[0];
    const box = get("detail");
    box.replaceChildren(el("div", p.id + " / PHASE INSPECTOR", "eyebrow"), el("h2", p.title), el("p", p.outcome));
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
        checks.append(el("summary", "Verification jobs at the recorded code revision"));
        for (const check of p.evidence.checks)
            checks.append(el("p", `${label(check.name)} · ${check.exit_code === 0 ? "passed" : "failed"} · ${check.duration_ms}ms`));
        checks.append(el("p", "A passing job does not establish every acceptance scenario or real-task efficacy. Read the acceptance matrix for the actual qualification scope.", "caption"));
        box.append(checks);
    }
    const source = el("p", p.approval_basis, "integrity");
    box.append(source, el("h3", "Acceptance scope"));
    const list = el("ul");
    for (const item of p.criteria)
        list.append(el("li", item));
    box.append(list, el("p", "Criterion-level execution is not instrumented in this adapter. Completed reports provide the frozen verification record; phase state alone does not mark these criteria passed.", "caption"));
    const actions = el("div", "", "actions");
    for (const url of p.evidence.links) {
        const a = el("a", url.endsWith("-pdf")
            ? "Read phase PDF"
            : url.endsWith("-epub")
                ? "Kindle edition"
                : label(url.split("-").slice(1).join("-")));
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
        box.append(el("p", "Verified code: " +
            p.evidence.code_revision +
            " · " +
            p.evidence.platform, "caption"));
    if (p.evidence.report === "invalid_or_missing")
        box.append(el("p", "Completion evidence is missing or changed. This phase is excluded from verified closeout totals.", "integrity"));
    for (const node of document.querySelectorAll("[data-phase]")) {
        const active = node.dataset.phase === p.id;
        node.classList.toggle("active", active);
        node.setAttribute("aria-pressed", String(active));
    }
}
function render() {
    if (!snapshot)
        return;
    const focused = document.activeElement;
    const focusParent = focused?.parentElement?.id;
    const focusPhase = focused?.dataset.phase;
    const focusHref = focused instanceof HTMLAnchorElement ? focused.href : null;
    get("login").hidden = true;
    get("content").hidden = false;
    get("notice").textContent = "";
    if (get("receipt-notice").textContent ===
        "Sign in to inspect retained receipts.")
        get("receipt-notice").textContent =
            "Choose a project to inspect retained receipts. No model usage is inferred.";
    for (const id of ["pause", "refresh", "logout"])
        get(id).hidden = false;
    get("freshness").textContent = paused
        ? "Updates paused · snapshot ages normally"
        : snapshot.streaming && !streamFallback
            ? "Current snapshot · live updates and 5s polling"
            : "Current snapshot · polled every 5s";
    renderCapacity(snapshot.capacity);
    get("development-record").hidden = snapshot.phases.length === 0;
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
    const pending = snapshot.phases.filter((p) => ["complete", "partial", "blocked"].includes(p.execution) &&
        p.evidence.report === "qa_passed" &&
        p.approval !== "Report approved").length;
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
        card.append(el("div", title, "eyebrow"), el("strong", value), el("p", note));
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
        node.append(el("strong", p.id), el("span", p.execution === "complete"
            ? "Closed"
            : p.execution === "in_progress"
                ? "Active"
                : p.execution === "partial"
                    ? "Partial"
                    : p.execution === "blocked"
                        ? "Blocked"
                        : p.execution === "closeout_pending"
                            ? "Reporting"
                            : "—"));
        node.title = p.title;
        node.setAttribute("aria-label", p.id + " " + p.title + " " + label(p.execution));
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
        for (const node of get(focusParent).querySelectorAll("[data-phase]")) {
            if (node.dataset.phase === focusPhase)
                node.focus({ preventScroll: true });
        }
    }
    else if (focusHref) {
        for (const node of get("content").querySelectorAll("a")) {
            if (node.href === focusHref) {
                node.focus({ preventScroll: true });
                break;
            }
        }
    }
}
async function refresh() {
    if (busy || loggedOut)
        return;
    busy = true;
    const ticket = generation;
    try {
        const value = (await api("/api/snapshot"));
        if (ticket !== generation)
            return;
        if (value.schema_version !== 1 || ![0, 12].includes(value.phases.length))
            throw new Error("Snapshot unavailable");
        if (snapshot &&
            (snapshot.incarnation !== value.incarnation ||
                snapshot.principal !== value.principal ||
                snapshot.tenant !== value.tenant ||
                snapshot.authorization_epoch !== value.authorization_epoch)) {
            clear("Authorization context changed.");
        }
        snapshot = value;
        render();
        startStream();
        void refreshExecution();
    }
    catch (error) {
        if (ticket === generation)
            clear("Session or source unavailable. " +
                (error instanceof Error ? label(error.message) : ""));
    }
    finally {
        busy = false;
    }
}
get("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    generation++;
    loggedOut = false;
    stopStream();
    streamFallback = false;
    try {
        await api("/api/bootstrap", { code: get("code").value });
        get("code").value = "";
        await refresh();
    }
    catch (error) {
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
    }
    catch {
        get("notice").textContent =
            "Local view cleared. Session termination was not confirmed; it expires within 15 minutes.";
    }
});
get("refresh").addEventListener("click", () => void refresh());
get("overview").addEventListener("click", () => window.scrollTo({ top: 0 }));
get("retrieval-nav").addEventListener("click", () => get("retrieval-lab").scrollIntoView());
get("receipt-project").addEventListener("input", clearReceipts);
get("receipt-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearReceipts();
    if (!snapshot)
        return;
    const ticket = receiptGeneration;
    const context = generation;
    const project = get("receipt-project").value;
    get("receipt-notice").textContent = "Checking current access…";
    try {
        const result = await api("/api/receipts/" + encodeURIComponent(project));
        if (!snapshot || ticket !== receiptGeneration || context !== generation)
            return;
        receiptExpires = Date.now() + 15000;
        const items = result.items;
        get("receipt-notice").textContent = items.length
            ? `${items.length} retained receipts · snapshot clears after 15 seconds`
            : "No currently available receipts for this owner and project.";
        for (const item of items) {
            const button = el("button", "", "receipt-row");
            button.append(el("strong", label(item.status)), el("small", `${item.usage.output_tokens ?? "Unknown"} tokens · ${item.usage.counting ?? "not counted"} · cache ${item.cache}`), el("small", new Date(item.created_ms).toLocaleString()));
            button.addEventListener("click", async () => {
                try {
                    const detail = await api("/api/receipt/" +
                        encodeURIComponent(project) +
                        "/" +
                        encodeURIComponent(item.receipt));
                    if (!snapshot ||
                        ticket !== receiptGeneration ||
                        context !== generation)
                        return;
                    receiptExpires = Date.now() + 15000;
                    const diag = detail.diagnostics;
                    const panel = get("receipt-detail");
                    const meter = el("meter");
                    meter.max = diag.usage.max_output_tokens || 1;
                    meter.value = diag.usage.output_tokens || 0;
                    meter.hidden = typeof diag.usage.output_tokens !== "number";
                    if (diag.status === "budget_insufficient")
                        meter.className = "budget-failed";
                    meter.setAttribute("aria-label", "Serialized token budget used");
                    panel.replaceChildren(el("div", "Response details", "eyebrow"), el("h3", label(diag.status)), el("p", `${diag.usage.output_tokens ?? "Unknown"} / ${diag.usage.max_output_tokens ?? "not applicable"} tokens · ${diag.usage.counting ?? "not counted for this artifact"}`), meter, el("p", `${diag.usage.serialized_bytes} serialized bytes · Provider usage: ${diag.usage.provider_usage}`));
                    if (diag.status === "budget_insufficient") {
                        panel.prepend(el("p", `No briefing fits. Minimum essential budget: ${diag.minimum_required_tokens ?? "not computable"} tokens. The displayed count measures only the failure response.`, "integrity"));
                    }
                    if (diag.coverage)
                        panel.append(el("p", `Channels: lexical ${diag.coverage.lexical}; dense ${diag.coverage.dense}. Optional coverage: ${label(diag.coverage.optional)}.`, "caption"));
                    for (const stage of diag.trace ?? []) {
                        const row = el("div", "", "trace-stage");
                        row.append(el("strong", label(String(stage.stage))));
                        const facts = el("div", "", "trace-facts");
                        for (const [key, value] of Object.entries(stage)) {
                            if (key === "stage")
                                continue;
                            facts.append(el("span", key === "duration_ms"
                                ? `Duration: ${value} ms`
                                : `${label(key)}: ${typeof value === "string" ? label(value) : String(value)}`));
                        }
                        row.append(facts);
                        panel.append(row);
                    }
                    const list = el("ul");
                    for (const selected of diag.selected ?? [])
                        list.append(el("li", `${label(selected.authority)} · ${label(selected.reason)} · ${selected.reference}`));
                    panel.append(list, el("p", `Omissions: ${(diag.omissions ?? []).join(", ") || "none recorded"}`, "caption"), el("code", "SHA-256 " + String(detail.rendered_hash)), el("p", "Inspecting this receipt does not execute anything. Exact replay rechecks permission on retained bytes; rerun compiles a new artifact from explicit inputs. Neither calls a model.", "caption"));
                    const replay = el("button", "Exact retained replay"), rerun = el("button", "Rerun retrieval with these inputs"), inputs = el("textarea"), output = el("pre");
                    inputs.setAttribute("aria-label", "New briefing inputs JSON");
                    inputs.rows = 8;
                    inputs.value = JSON.stringify({
                        project,
                        query: "inspect current evidence",
                        bindings: [{}],
                        max_output_tokens: 6000,
                        tokenizer: "cl100k_base",
                    }, null, 2);
                    const invoke = async (action) => {
                        if (!snapshot ||
                            ticket !== receiptGeneration ||
                            context !== generation)
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
                                    args: action === "replay"
                                        ? { project, receipt: item.receipt }
                                        : {
                                            project,
                                            predecessor: item.receipt,
                                            brief: JSON.parse(inputs.value),
                                        },
                                },
                            });
                            if (!snapshot ||
                                ticket !== receiptGeneration ||
                                context !== generation)
                                return;
                            output.textContent =
                                (action === "replay"
                                    ? "Exact retained artifact (displayed as formatted JSON)."
                                    : "New retrieval artifact linked to the selected predecessor; results may differ.") +
                                    "\n" +
                                    JSON.stringify(result, null, 2);
                            // Never extend the displayed snapshot's original lifetime.
                        }
                        catch (error) {
                            if (ticket !== receiptGeneration || context !== generation)
                                return;
                            output.textContent = `Unavailable: ${error instanceof Error ? error.message : "request failed"}. Reload before another attempt.`;
                        }
                        finally {
                            replay.disabled = rerun.disabled = false;
                        }
                    };
                    replay.onclick = () => void invoke("replay");
                    rerun.onclick = () => void invoke("rerun");
                    const rerunBox = el("details");
                    rerunBox.append(el("summary", "New retrieval inputs (no model call)"), inputs, rerun);
                    panel.append(replay, rerunBox, output);
                }
                catch {
                    clearReceipts();
                    get("receipt-notice").textContent =
                        "Receipt unavailable under current permissions. Reload to check again.";
                }
            });
            get("receipt-list").append(button);
        }
    }
    catch {
        if (ticket !== receiptGeneration || context !== generation)
            return;
        clearReceipts();
        get("receipt-notice").textContent =
            "Receipts unavailable. Check the project and current session.";
    }
});
get("pause").addEventListener("click", () => {
    paused = !paused;
    get("pause").textContent = paused ? "Resume updates" : "Pause updates";
    get("freshness").textContent = paused
        ? "Updates paused · snapshot ages normally"
        : "Refreshing";
    if (paused)
        stopStream();
    if (!paused)
        void refresh();
});
get("theme").addEventListener("click", () => document.body.classList.toggle("light"));
setInterval(() => {
    observatory.tick();
    if (executionExpires && Date.now() >= executionExpires) {
        clearExecution("Task snapshot expired. Reconnect to check current permissions.");
    }
    if (receiptExpires && Date.now() >= receiptExpires) {
        clearReceipts();
        get("receipt-notice").textContent =
            "Receipt snapshot expired. Load again to recheck permissions.";
    }
    if (snapshot && Date.now() > snapshot.fresh_until_ms) {
        clear("Snapshot expired. Refresh checks current authorization before displaying content.");
        get("refresh").hidden = false;
    }
}, 250);
setInterval(() => {
    if (!paused && !document.hidden)
        void refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
        generation++;
        clear("View cleared while hidden. Reconnecting checks current permission.");
    }
    else if (!loggedOut)
        void refresh();
});
let executionProject = "";
let executionGeneration = 0;
let executionExpires = 0;
let executionBusy = false;
function clearExecution(message = "Enter a project to load its tasks.") {
    executionGeneration++;
    executionExpires = 0;
    executionProject = "";
    get("execution-tasks").replaceChildren();
    get("execution-events").replaceChildren();
    get("execution-provenance").textContent = "";
    get("execution-notice").textContent = message;
}
function renderExecution(value) {
    const tasks = get("execution-tasks");
    tasks.replaceChildren();
    get("execution-notice").textContent = value.tasks.length
        ? `${value.tasks.length} task record${value.tasks.length === 1 ? "" : "s"} · refreshed every 5s while updates run`
        : "No tasks in the current project view.";
    for (const task of value.tasks) {
        const card = el("article", "", "task-card");
        card.append(el("div", `Saved by ${task.writer} · Task ${task.task}${task.run ? ` · Run ${task.run}` : ""}`, "eyebrow"), el("h3", task.objective), el("p", task.current_state));
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
        card.append(bar, el("p", summary, "criterion-summary"), el("p", `Weighted: ${p.weighted.passed} passed / ${p.weighted.total} declared units; ${p.weighted.waived} waived, ${p.weighted.unknown} unknown. Missing weights default to 1.`, "caption"));
        if (p.scope_changed)
            card.append(el("p", `Scope changed since previous checkpoint: ${p.previous_total} → ${p.total} criteria.`, "integrity"));
        if (p.reopened)
            card.append(el("p", `${p.reopened} previously passed criteria reopened since previous checkpoint.`, "integrity"));
        if (!p.history_complete)
            card.append(el("p", "Definition history is incomplete; passed claims remain unknown.", "integrity"));
        const heartbeat = task.heartbeat;
        card.append(el("p", heartbeat.status === "recent"
            ? `Heartbeat observed ${new Date(heartbeat.recorded_ms).toLocaleTimeString()} · reported ${label(heartbeat.activity ?? "unknown")}`
            : `Heartbeat ${heartbeat.status} · current liveness unknown`, "heartbeat " + heartbeat.status));
        if (task.blockers.length) {
            const blockers = el("ul", "", "blocker-list");
            for (const b of task.blockers)
                blockers.append(el("li", `${b.text} · owner: ${b.owner ?? "unassigned"}`));
            card.append(el("h4", "Reported blockers"), blockers);
        }
        const details = el("details");
        details.dataset.task = task.task + "/" + (task.run ?? "task");
        details.append(el("summary", "Inspect criteria, evidence and source"));
        for (const c of task.criteria) {
            const row = el("div", "", "criterion-row");
            row.append(el("strong", `${c.id} · ${c.effective}`), el("p", c.text));
            if (c.reported !== c.effective)
                row.append(el("p", `Producer reported ${c.reported}; current evidence or definition checks prevent credit.`, "integrity"));
            if (c.reverification_required)
                row.append(el("p", "Reverification required: changed definition with reused proof, or incomplete history.", "caption"));
            for (const e of c.evidence)
                row.append(el("code", `${e.kind}: ${e.id}`));
            if (c.decision)
                row.append(el("p", `Waiver decision: ${c.decision}`, "caption"));
            details.append(row);
        }
        for (const v of task.verification)
            details.append(el("p", `${v.name}: ${label(v.result)} · ${v.environment} · revision ${v.target_revision ?? "unspecified"}`, "caption"));
        for (const op of task.operations)
            details.append(el("p", `${op.id}: ${label(op.state)}${op.reconciliation_required ? " · reconciliation required" : ""}`, "caption"));
        for (const gap of task.capture_gaps)
            details.append(el("p", "Capture gap: " + gap, "integrity"));
        details.append(el("p", `Checkpoint ${task.checkpoint} · revision ${task.revision} · writer ${task.writer} · position ${task.source_position}`, "caption"), el("code", "Body SHA-256 " + task.body_hash), el("code", "Criterion scope " + p.scope_hash), el("p", "Display text is bounded. Use full resume for complete task state. Repository freshness must be inspected against the current snapshot.", "caption"));
        card.append(details);
        tasks.append(card);
    }
    const events = get("execution-events");
    events.replaceChildren();
    for (const event of value.events) {
        const row = el("li");
        row.append(el("strong", label(event.kind)), el("p", `${event.task} · ${event.actor}`, "caption"), el("p", `Recorded ${new Date(event.recorded_ms).toLocaleString()}`, "caption"), el("p", `Observed ${new Date(event.observed_ms).toLocaleString()} · position ${event.source_position}`, "caption"));
        events.append(row);
    }
    if (!value.events.length)
        events.append(el("li", "No linked activity records visible."));
    get("execution-provenance").textContent =
        `${value.project} · ${value.tenant} / ${value.principal} · history ${value.incarnation} · observed ${new Date(value.observed_ms).toLocaleString()}. Bounded view: up to 8 visible tasks from 64 recent streams, 32 events from 128 candidates, 128 prior checkpoints per stream. No hidden totals, private reasoning, cost or ETA.`;
}
async function refreshExecution() {
    if (!snapshot || !executionProject || executionBusy)
        return;
    executionBusy = true;
    const ticket = executionGeneration, context = generation, project = executionProject;
    const started = Date.now();
    try {
        const value = (await api("/api/execution/" + encodeURIComponent(project)));
        if (ticket !== executionGeneration || context !== generation || !snapshot)
            return;
        if (value.schema_version !== 1 ||
            value.status !== "available" ||
            value.project !== project ||
            value.incarnation !== snapshot.incarnation ||
            value.tenant !== snapshot.tenant ||
            value.principal !== snapshot.principal ||
            Date.now() >= started + 15000)
            throw new Error("Current task authorization or evidence unavailable");
        executionExpires = Math.min(started + 15000, value.fresh_until_ms);
        if (Date.now() >= executionExpires)
            throw new Error("Snapshot expired");
        // Preserve disclosure state and keyboard position across observational polling.
        const open = new Set(Array.from(get("execution-tasks").querySelectorAll("details[open]"), (d) => d.dataset.task));
        const focused = document.activeElement?.closest("details")?.dataset.task;
        renderExecution(value);
        for (const d of get("execution-tasks").querySelectorAll("details")) {
            d.open = open.has(d.dataset.task);
            if (focused === d.dataset.task)
                d.querySelector("summary")?.focus({ preventScroll: true });
        }
    }
    catch {
        if (ticket === executionGeneration && context === generation)
            clearExecution("Task data unavailable. Reconnect to recheck project access and current evidence.");
    }
    finally {
        executionBusy = false;
    }
}
get("execution-nav").addEventListener("click", () => get("execution-panel").scrollIntoView());
get("execution-project").addEventListener("input", () => clearExecution());
get("execution-form").addEventListener("submit", (event) => {
    event.preventDefault();
    clearExecution();
    if (!snapshot)
        return;
    executionProject = get("execution-project").value;
    get("execution-notice").textContent = "Checking current task access…";
    void refreshExecution();
});
void refresh();
