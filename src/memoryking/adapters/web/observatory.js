import { AtlasView } from "./graph.js";
const e = (tag, text = "", cls = "") => {
    const n = document.createElement(tag);
    n.textContent = text;
    n.className = cls;
    return n;
};
const doc = (v) => v !== null && typeof v === "object" && !Array.isArray(v) ? v : {};
const rows = (v) => (Array.isArray(v) ? v.map(doc) : []);
const readable = (v) => v === null || v === undefined
    ? "Unknown"
    : typeof v === "object"
        ? JSON.stringify(v)
        : String(v);
function markdown(text) {
    // A small, safe renderer for the briefing's own Markdown: headings, list items,
    // emphasis markers stripped, every node built from text content only.
    const nodes = [];
    let list = null;
    const plain = (line) => line.replace(/\*\*|__|(^|\s)_(.+?)_(?=\s|$)/g, "$1$2");
    for (const raw of text.split("\n")) {
        const line = raw.trimEnd();
        if (!line) {
            list = null;
            continue;
        }
        const heading = /^(#{1,3})\s+(.*)$/.exec(line);
        if (heading) {
            list = null;
            nodes.push(e(heading[1].length === 1 ? "h3" : "h4", plain(heading[2])));
            continue;
        }
        const item = /^(?:[-*]|\d+\.)\s+(.*)$/.exec(line);
        if (item) {
            if (!list) {
                list = e(/^\d/.test(line) ? "ol" : "ul");
                nodes.push(list);
            }
            list.append(e("li", plain(item[1])));
            continue;
        }
        list = null;
        nodes.push(e("p", plain(line), line.startsWith("_") ? "caption" : ""));
    }
    return nodes;
}
function detail(title, value) {
    const d = e("details");
    d.append(e("summary", title), e("pre", JSON.stringify(value, null, 2)));
    return d;
}
function button(text, action) {
    const b = e("button", text);
    b.type = "button";
    b.onclick = action;
    return b;
}
function field(parent, title, value = "", kind = "text") {
    const label = e("label", title), input = e("input");
    input.type = kind;
    input.value = value;
    input.autocomplete = "off";
    input.maxLength = 256;
    label.append(input);
    parent.append(label);
    return input;
}
function table(items, keys) {
    const wrap = e("div", "", "table-scroll"), t = e("table"), h = e("tr");
    for (const k of keys)
        h.append(e("th", k.replaceAll("_", " ")));
    t.append(h);
    for (const row of items) {
        const tr = e("tr");
        for (const k of keys)
            tr.append(e("td", readable(row[k])));
        t.append(tr);
    }
    wrap.append(t);
    if (!items.length)
        wrap.append(e("p", "No authorized records in this window."));
    return wrap;
}
/** In-memory views are short lived and bound to the exact browser authority. */
export class Observatory {
    api;
    current;
    openReceipt;
    root;
    status;
    output;
    project;
    bindings;
    view;
    ticket = 0;
    expires = 0;
    expiresMono = 0;
    nextRefresh = 0;
    lastWall = Date.now();
    lastMono = performance.now();
    cycle = null;
    reader = null;
    signature = "";
    graph = null;
    context = "";
    confirmed = null;
    writing = false;
    lastRead = null;
    constructor(root, api, current, openReceipt = () => { }) {
        this.api = api;
        this.current = current;
        this.openReceipt = openReceipt;
        this.root = root;
        root.append(e("h2", "Memory"), e("p", "Browse stored memories, checkpoints, reviews and processing usage. Active views refresh every five seconds while updates run, with current permission checks."));
        const form = e("form", "", "observatory-form");
        this.project = field(form, "Project", "", "text");
        this.project.required = true;
        this.project.pattern = "[A-Za-z0-9_\\-]{1,96}";
        const label = e("label", "View");
        this.view = e("select");
        this.view.setAttribute("aria-label", "View");
        for (const [id, name] of [
            ["brief", "Ask for a briefing"],
            ["timeline", "Timeline"],
            ["atlas", "Memory graph"],
            ["history", "Checkpoint history"],
            ["review", "Review & conflicts"],
            ["cost", "Usage & costs"],
            ["operations", "Jobs & health"],
            ["handoffs", "Handoffs & checkpoints"],
            ["impact", "Change impact"],
            ["influence", "Where did this go"],
            ["retention", "Retention & purge"],
        ]) {
            const o = e("option", name);
            o.value = id;
            this.view.append(o);
        }
        label.append(this.view);
        form.append(label);
        const advanced = e("details"), bl = e("label", "Applicability bindings (JSON array)");
        this.bindings = e("textarea");
        this.bindings.value = "[{}]";
        this.bindings.rows = 3;
        this.bindings.maxLength = 16384;
        bl.append(this.bindings);
        advanced.append(e("summary", "Task and environment scope"), bl, e("p", "Supply known dimensions such as path, environment or version. Missing dimensions remain unknown and may block constraints; they are never treated as permission.", "caption"));
        form.append(advanced, e("button", "Load current view", "primary"));
        this.status = e("p", "Choose a project and view.");
        this.status.setAttribute("role", "status");
        this.output = e("div", "", "observatory-output");
        root.append(form, this.status, this.output);
        for (const input of [this.project, this.bindings, this.view])
            input.addEventListener("input", () => this.clearResult("Scope changed. Load again to check current access."));
        form.onsubmit = (ev) => {
            ev.preventDefault();
            void this.load();
        };
        for (const type of ["pointerdown", "keydown", "click", "beforeinput"])
            root.addEventListener(type, (event) => {
                const ticket = this.ticket;
                this.tick(false);
                if (ticket !== this.ticket) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                }
            }, true);
    }
    clear() {
        this.clearResult("Session changed. Reconnect and select a project.");
        this.project.value = "";
        this.bindings.value = "[{}]";
        this.context = "";
    }
    clearResult(message) {
        this.ticket++;
        this.expires = 0;
        this.expiresMono = 0;
        this.cycle = null;
        this.reader = null;
        this.signature = "";
        this.confirmed = null;
        this.lastRead = null;
        this.graph?.clear();
        this.graph = null;
        this.output.replaceChildren();
        this.status.textContent = message;
    }
    clockValid() {
        const wall = Date.now(), mono = performance.now();
        if (wall < this.lastWall || mono < this.lastMono)
            return false;
        this.lastWall = wall;
        this.lastMono = mono;
        return true;
    }
    tick(refresh = true) {
        if (!this.clockValid()) {
            this.clearResult("Clock changed. Reload after the clock is stable.");
            return;
        }
        const ctx = this.current();
        if (this.context && (!ctx || JSON.stringify(ctx) !== this.context)) {
            this.clear();
            return;
        }
        if (this.expires &&
            (document.hidden ||
                Date.now() >= this.expires ||
                performance.now() >= this.expiresMono)) {
            this.clearResult("View expired or hidden. Load again to check current access.");
            return;
        }
        if (refresh &&
            this.reader &&
            this.expires &&
            !this.cycle &&
            !this.writing &&
            !document.hidden &&
            performance.now() >= this.nextRefresh)
            void this.run(this.reader, true, true);
    }
    /** Only skip DOM replacement after every read for this view has reauthorized. */
    unchanged(...values) {
        const signature = JSON.stringify(values.map((value) => Object.fromEntries(Object.entries(value).filter(([key]) => !["receipt", "evaluated_ms", "fresh_until_ms"].includes(key)))));
        const same = this.cycle?.refreshing && signature === this.signature;
        this.signature = signature;
        return Boolean(same);
    }
    scope() {
        const b = JSON.parse(this.bindings.value);
        if (!Array.isArray(b) ||
            !b.length ||
            b.length > 256 ||
            b.some((x) => !x || typeof x !== "object" || Array.isArray(x)))
            throw Error("Bindings must be a bounded array of objects");
        return b;
    }
    async command(operation, action, args = {}, key, bare = false) {
        const ctx = this.current();
        if (!ctx || document.hidden || !this.clockValid())
            throw Error("Current visible session required");
        const stamp = JSON.stringify(ctx), ticket = this.ticket, project = this.project.value, started = Date.now(), startedMono = performance.now(), cycle = this.cycle;
        // The inference contract binds history inside its arguments, not on the command.
        const command = bare
            ? { version: 1, action, args: { ...args, project } }
            : {
                version: 1,
                incarnation: ctx.incarnation,
                action,
                args: { ...args, project },
            };
        if (key)
            command.key = key;
        const result = await this.api("/api/command", {
            csrf: ctx.csrf,
            operation,
            command,
        });
        if (ticket !== this.ticket ||
            JSON.stringify(this.current()) !== stamp ||
            project !== this.project.value ||
            document.hidden ||
            !this.clockValid() ||
            performance.now() >= startedMono + 15000 ||
            Date.now() >= started + 15000)
            throw Error("View changed while checking access");
        this.context = stamp;
        const deadline = typeof result.fresh_until_ms === "number"
            ? result.fresh_until_ms
            : started + 15000;
        const expires = Math.min(started + 15000, deadline), monotonic = Math.min(startedMono + 15000, performance.now() + expires - Date.now());
        if (Date.now() >= expires || performance.now() >= monotonic)
            throw Error("Snapshot expired");
        if (cycle) {
            cycle.expires = Math.min(cycle.expires, expires);
            cycle.monotonic = Math.min(cycle.monotonic, monotonic);
        }
        else {
            // A one-off export or confirmation cannot renew unrelated retained content.
            this.expires = Math.min(this.expires || Infinity, expires);
            this.expiresMono = Math.min(this.expiresMono || Infinity, monotonic);
        }
        this.status.textContent = `Current permission checked · ${project} · ${ctx.tenant} / ${ctx.principal} · history ${ctx.incarnation}.`;
        return result;
    }
    async run(action, wholeView = false, refreshing = false) {
        if (wholeView) {
            this.ticket++;
            this.cycle = {
                ticket: this.ticket,
                expires: Infinity,
                monotonic: Infinity,
                refreshing,
            };
        }
        const ticket = this.ticket;
        const cycle = wholeView ? this.cycle : null;
        try {
            await action();
            if (cycle && cycle === this.cycle && ticket === this.ticket) {
                if (!this.clockValid() ||
                    document.hidden ||
                    Date.now() >= cycle.expires ||
                    performance.now() >= cycle.monotonic)
                    throw Error("Snapshot expired");
                if (Number.isFinite(cycle.expires)) {
                    this.expires = cycle.expires;
                    this.expiresMono = cycle.monotonic;
                }
                this.nextRefresh = performance.now() + 5000;
            }
        }
        catch (error) {
            if (ticket === this.ticket) {
                this.clearResult("Unavailable: " +
                    (error instanceof Error
                        ? error.message.replaceAll("_", " ")
                        : "request failed") +
                    ". Reload to reconcile; mutations are never automatically retried.");
            }
        }
        finally {
            if (cycle && cycle === this.cycle)
                this.cycle = null;
        }
    }
    async load() {
        const started = performance.now();
        this.clearResult("Checking current permission…");
        await this.run(async () => {
            switch (this.view.value) {
                case "brief":
                    this.brief();
                    break;
                case "timeline":
                    await this.timeline();
                    break;
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
                case "influence":
                    this.influence();
                    break;
                case "retention":
                    this.retention();
                    break;
            }
        }, true);
        requestAnimationFrame(() => {
            this.root.dataset.viewReadyMs = String(Math.round(performance.now() - started));
        });
    }
    exportCurrent(load) {
        this.lastRead = load;
        const exportButton = button("Export current authorized JSON", () => void this.run(async () => {
            // Re-fetch through the same authorization gate; never serialize a stale DOM cache.
            if (!this.lastRead)
                throw Error("Reload required");
            const value = await this.lastRead(), url = URL.createObjectURL(new Blob([
                JSON.stringify({
                    exported_ms: Date.now(),
                    authorization: "current_at_export",
                    data: value,
                }, null, 2),
            ], { type: "application/json" }));
            const a = e("a");
            a.href = url;
            a.download = "memoryking-authorized-view.json";
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 0);
        }));
        exportButton.dataset.export = "true";
        this.output.append(exportButton);
    }
    async atlas(focus, cursor) {
        this.reader = () => this.atlas(focus, cursor);
        const args = {
            bindings: this.scope(),
            max_nodes: focus ? 500 : 200,
            max_edges: focus ? 1500 : 400,
        };
        if (focus)
            args.focus = [focus];
        if (cursor)
            args.cursor = cursor;
        const load = () => this.command("retrieval", "atlas", args), data = await load();
        if (!this.graph) {
            this.output.replaceChildren(e("h3", "Memory relationships"));
            const host = e("div");
            this.output.append(host);
            this.graph = new AtlasView(host, (id) => void this.run(() => this.atlas(id)), {
                load: async (id) => {
                    const project = this.project.value;
                    const data = await this.api("/api/citations/" +
                        encodeURIComponent(project) +
                        "/" +
                        encodeURIComponent(id));
                    return (Array.isArray(data.items) ? data.items : []);
                },
                open: (receipt) => this.openReceipt(this.project.value, receipt),
            });
        }
        this.graph.set(data);
        this.output.querySelector(".atlas-footer")?.remove();
        this.output.querySelector("[data-export]")?.remove();
        const footer = e("div", "", "atlas-footer");
        if (data.next_cursor)
            footer.append(button("Next bounded page", () => void this.run(() => this.atlas(focus, String(data.next_cursor)), true)));
        if (focus)
            footer.append(button("Return to overview", () => void this.run(() => this.atlas(), true)));
        this.output.append(footer);
        this.exportCurrent(load);
    }
    async history(position, baseline) {
        this.reader = () => this.history(position, baseline);
        const load = () => this.command("inspection", "history", position === undefined ? {} : { recorded_position: position });
        const data = await load();
        const previous = baseline === undefined
            ? null
            : await this.command("inspection", "history", {
                recorded_position: baseline,
            });
        if (this.unchanged(data, ...(previous ? [previous] : [])))
            return;
        this.output.replaceChildren(e("h3", position === undefined
            ? "Live checkpoint state"
            : `Historical state at position ${position}`));
        this.output.append(e("p", "Current permissions apply to every frame. Moving through history never invokes a tool, replays a mutation or changes live task state. Operation history and historical liveness are unavailable.", "integrity"));
        const frames = rows(data.frames), controls = e("div", "", "history-controls");
        const pick = e("select");
        pick.setAttribute("aria-label", "Historical checkpoint");
        for (const f of frames) {
            const o = e("option", `${f.task} · r${f.revision} · ${new Date(Number(f.recorded_ms)).toLocaleString()}`);
            o.value = String(f.position);
            pick.append(o);
        }
        if (position !== undefined)
            pick.value = String(position);
        const base = e("select");
        base.setAttribute("aria-label", "Comparison baseline");
        base.append(e("option", "No comparison"));
        base.options[0].value = "";
        for (const f of frames) {
            const o = e("option", `${f.task} · position ${f.position}`);
            o.value = String(f.position);
            base.append(o);
        }
        if (baseline !== undefined)
            base.value = String(baseline);
        controls.append(pick, base, button("Inspect selected frame", () => void this.run(() => this.history(Number(pick.value), base.value ? Number(base.value) : undefined), true)), button("Return to live", () => void this.run(() => this.history(), true)));
        this.output.append(controls);
        if (frames.length) {
            const slider = e("input");
            slider.type = "range";
            slider.min = "0";
            slider.max = String(frames.length - 1);
            slider.value = String(Math.max(0, frames.findIndex((f) => f.position === position)));
            slider.setAttribute("aria-label", "Checkpoint timeline");
            slider.onchange = () => {
                const next = Number(frames[Number(slider.value)].position);
                void this.run(() => this.history(next, base.value ? Number(base.value) : undefined), true);
            };
            this.output.append(slider);
        }
        else
            this.output.append(e("p", "No currently accessible checkpoints."));
        const projection = doc(data.projection), tasks = rows(projection.tasks);
        this.output.append(this.taskTable(tasks));
        if (previous) {
            const before = rows(doc(previous.projection).tasks), changes = [];
            const all = [
                ...tasks,
                ...before.filter((old) => !tasks.some((t) => t.task === old.task && t.run === old.run)),
            ];
            for (const task of all) {
                const current = tasks.find((t) => t.task === task.task && t.run === task.run);
                const old = before.find((t) => t.task === task.task && t.run === task.run), p = doc(current?.progress), b = doc(old?.progress);
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
            this.output.append(e("h3", "Checkpoint comparison"), e("p", "Changed denominators reflect scope edits. A higher fraction alone is not evidence of progress."), table(changes, [
                "task",
                "run",
                "scope_changed",
                "passed_before",
                "passed_now",
                "total_before",
                "total_now",
            ]));
        }
        this.output.append(detail("Full authorized historical projection", data));
        this.exportCurrent(load);
    }
    taskTable(tasks) {
        return table(tasks.map((t) => ({
            ...t,
            passed: doc(t.progress).passed,
            total: doc(t.progress).total,
            heartbeat: doc(t.heartbeat).status,
        })), ["task", "run", "revision", "writer", "passed", "total", "heartbeat"]);
    }
    async review() {
        this.reader = () => this.review();
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
        const ranking = await this.command("experience", "ranking_inspect");
        const held = await this.command("inspection", "held");
        const rules = await this.command("inspection", "trust_list");
        if (this.unchanged(knowledge, constraints, procedures, ranking, held, rules))
            return;
        this.output.replaceChildren(e("h3", "Review queue & evidence quality"), e("p", "Source content is untrusted evidence. Review changes lifecycle authority; procedure activation is separate and grants no execution privileges. A policy change requires a new browser sign-in.", "integrity"));
        for (const [name, data, operation] of [
            ["Knowledge", knowledge, "knowledge"],
            ["Constraints", constraints, "knowledge"],
            ["Procedures", procedures, "experience"],
        ]) {
            const section = e("section");
            section.append(e("h3", name), e("p", `Resolution: ${readable(data.status)} · ${readable(data.reason)}`));
            for (const item of rows(data.items)) {
                const card = e("article", "", "review-card"), body = doc(item.body);
                card.append(e("h4", String(body.text ?? body.name ?? item.revision)), e("p", `${readable(item.state ?? item.lifecycle)} · ${readable(item.authority)} · version ${item.family_version}`), detail("Applicability, sources, conflicts and attestations", item));
                const controls = e("div", "", "actions");
                for (const decision of [
                    "accept",
                    "reject",
                    "deprecate",
                    "quarantine",
                    "release_quarantine",
                ])
                    controls.append(button(decision.replaceAll("_", " "), () => this.reviewAction(operation, "review", item, {
                        decision,
                        breadth_reviewed: false,
                    })));
                if (operation === "experience") {
                    controls.append(button("Activate", () => this.reviewAction(operation, "activate", item, {
                        bindings,
                        policy: "reviewed-procedure-v1",
                    })), button("Suspend", () => this.reviewAction(operation, "suspend", item, {})));
                }
                card.append(controls);
                section.append(card);
            }
            if (!rows(data.items).length)
                section.append(e("p", "No authorized matching revisions."));
            section.append(detail("Resolution and completeness details", data));
            this.output.append(section);
        }
        this.output.append(detail("Active ranking and evaluation evidence", ranking));
        this.output.append(this.heldSection(held, rules));
        const drafting = e("section");
        drafting.append(e("h3", "Draft proposals from repeated evidence"), e("p", "Files a proposal for every name that several independent observations share, with one evidence span per source. Drafts wait here for review like any proposal; nothing becomes knowledge by itself.", "integrity"), button("Draft proposals", () => {
            void this.run(async () => {
                await this.mutate("knowledge", "draft", { limit: 16 });
            });
        }));
        this.output.append(drafting);
        this.composer();
    }
    reviewAction(operation, action, item, extra) {
        const revision = item.revision, expected_version = item.family_version;
        const actionLabel = String(extra.decision ?? action).replaceAll("_", " ");
        const form = e("form", "", "review-confirm");
        form.append(e("h3", `${actionLabel} revision ${revision}`), e("p", `Compare-and-set family version ${expected_version}. Check the source and scope above before confirming. The server rechecks current authority and version.`));
        const reason = field(form, "Decision reason (identifier)", "reviewed_in_dashboard");
        reason.required = true;
        reason.pattern = "[A-Za-z0-9_\\-]{1,96}";
        const breadth = field(form, "I reviewed the applicability breadth", "", "checkbox");
        form.append(e("button", "Confirm " + actionLabel, "primary"), button("Cancel", () => form.remove()));
        form.onsubmit = (event) => {
            event.preventDefault();
            void this.run(async () => {
                const args = {
                    revision,
                    expected_version,
                    ...extra,
                    reason: reason.value,
                };
                if (action === "review")
                    args.breadth_reviewed = breadth.checked;
                await this.mutate(operation, action, args);
            });
        };
        this.output.querySelector(".review-confirm")?.remove();
        this.output.append(form);
        reason.focus();
    }
    /** Held captures from untrusted sources, with release buttons and the trust rules. */
    heldSection(held, rules) {
        const section = e("section");
        const items = rows(held.held);
        section.append(e("h3", "Held captures"), e("p", "Captures from a source marked untrusted are recorded but invisible to every read, briefing, graph and review until released here. Releasing is a journaled policy decision.", "integrity"));
        if (!items.length)
            section.append(e("p", "No held captures in this project."));
        for (const item of items) {
            const card = e("article", "", "review-card");
            card.append(e("h4", String(item.preview)), e("p", `${String(item.observation)} · ${String(item.source)} by ${String(item.captured_by)} · ${readable(item.reason)} · held ${new Date(Number(item.held_ms)).toLocaleString()}`));
            const release = button("Release", () => {
                void this.run(async () => {
                    await this.mutate("inspection", "release", {
                        observation: String(item.observation),
                    });
                });
            });
            card.append(release);
            section.append(card);
        }
        const ruleList = rows(rules.rules);
        section.append(e("h4", "Trust rules"), e("p", ruleList.length
            ? ruleList
                .map((r) => `${String(r.project)} · ${String(r.source)} · ${String(r.level)} (by ${String(r.decided_by)})`)
                .join(" · ")
            : "No rules; every source is trusted."));
        const f = e("form", "", "observatory-form"), source = field(f, "Source name or glob"), level = e("select");
        for (const value of ["untrusted", "trusted"]) {
            const option = e("option", value);
            option.value = value;
            level.append(option);
        }
        level.setAttribute("aria-label", "Trust level");
        source.required = true;
        f.append(level, e("button", "Set rule"));
        f.onsubmit = (ev) => {
            ev.preventDefault();
            void this.run(async () => {
                await this.mutate("inspection", "trust", {
                    source: source.value,
                    level: level.value,
                });
            });
        };
        section.append(f);
        return section;
    }
    async mutate(operation, action, args) {
        if (this.writing)
            throw Error("A command is already pending");
        // Never replay writes or let a background read repaint their result.
        const ticket = ++this.ticket;
        this.cycle = null;
        this.reader = null;
        this.writing = true;
        let result;
        try {
            result = await this.command(operation, action, args, crypto.randomUUID());
        }
        catch (error) {
            if (ticket === this.ticket)
                this.clearResult("Command result unavailable. Reload to reconcile; the command will not be retried automatically.");
            throw error;
        }
        finally {
            this.writing = false;
        }
        this.graph?.clear();
        this.output.replaceChildren(e("h3", "Command acknowledged"), detail("Recorded result", result), e("p", "Reload to reconcile current state. If this command changed authorization, sign in again. Acknowledgment does not approve a phase or start an agent."));
    }
    composer() {
        const d = e("details");
        d.append(e("summary", "Advanced proposal / attestation"), e("p", "Paste the documented command arguments for a knowledge or constraint proposal, an attestation, or a revision review with its expected family version. The preview is inert; confirmation sends one validated command. No source text is executed."));
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
        d.append(select, input, button("Preview command", () => {
            try {
                const args = JSON.parse(input.value);
                if (!args || Array.isArray(args) || typeof args !== "object")
                    throw Error("Object required");
                this.confirmed = {
                    operation: "knowledge",
                    action: select.value,
                    args,
                };
                preview.replaceChildren(detail("Exact proposed change", {
                    ...this.confirmed,
                    project: this.project.value,
                }), button("Confirm this command", () => void this.run(async () => {
                    const c = this.confirmed;
                    if (!c)
                        throw Error("Preview again");
                    this.confirmed = null;
                    await this.mutate(String(c.operation), String(c.action), doc(c.args));
                })));
            }
            catch {
                preview.replaceChildren(e("p", "Enter a valid JSON object."));
            }
        }), preview);
        this.output.append(d);
    }
    async cost() {
        this.reader = () => this.cost();
        const load = () => this.command("inspection", "cost"), data = await load(), summary = doc(data.processing);
        if (this.unchanged(data))
            return;
        this.output.replaceChildren(e("h3", "Processing attempts"), e("p", "Known usage is a lower bound when any attempts are unknown. Failed calls, retries and background processing remain included. Provider reports and actor outcomes are not independent metering.", "integrity"));
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
        const groups = [];
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
        const chart = e("div", "", "cost-bars"), max = Math.max(1, ...groups.map((g) => Number(g.known_tokens)));
        for (const g of groups) {
            const row = e("div");
            row.append(e("span", `${g.provider} · ${readable(g.stage)}: ≥${g.known_tokens} tokens (${g.unknown} unknown)`));
            const meter = e("meter");
            meter.min = 0;
            meter.max = max;
            meter.value = Number(g.known_tokens);
            meter.setAttribute("aria-label", `${readable(g.stage)} known token lower bound`);
            row.append(meter);
            chart.append(row);
        }
        this.output.append(chart, table(groups, [
            "provider",
            "model",
            "stage",
            "attempts",
            "known_tokens",
            "unknown",
            "cost",
        ]), detail("All retained authorized attempts", data.attempts), detail("Actor-reported outcomes, separate from usage", data.outcomes), detail("Coverage and accounting uncertainty", data));
        this.exportCurrent(load);
    }
    async operations() {
        this.reader = () => this.operations();
        const load = () => this.command("inspection", "operations"), data = await load();
        if (this.unchanged(data))
            return;
        this.output.replaceChildren(e("h3", "Jobs & primary health"), e("p", readable(data.health)), table(rows(data.jobs), [
            "id",
            "state",
            "position",
            "attempts",
            "fence",
            "lease_until",
            "available",
            "age_ms",
        ]), detail("Projection frontiers, coverage and measured fields", data), e("p", "Frontiers are opaque ordering tokens, not item counts. Lock wait, cache churn and recovery qualification remain unknown here. Retry, cancel and schedule controls are unavailable.", "integrity"));
        await this.egress();
        this.exportCurrent(load);
    }
    /** What left this store for a model provider, under which policy, and whether it arrived. */
    async egress() {
        const section = e("section");
        section.append(e("h3", "Egress audit"));
        let data;
        try {
            data = await this.command("inference", "audit", { before: null }, undefined, true);
        }
        catch (error) {
            section.append(e("p", "Egress audit unavailable under this session: " +
                (error instanceof Error
                    ? readable(error.message)
                    : "request failed"), "caption"));
            this.output.append(section);
            return;
        }
        const items = rows(data.items).map((item) => ({
            when: new Date(Number(item.evaluated_ms)).toLocaleString(),
            status: item.status,
            provider: item.provider,
            model: item.model,
            host: item.host,
            classification: item.classification,
            bytes: item.request_bytes,
            receipts: Array.isArray(item.receipts) ? item.receipts.length : 0,
            tokens: `${String(item.input_tokens ?? "?")} in / ${String(item.output_tokens ?? "?")} out`,
            permit: item.permit,
        }));
        section.append(e("p", `Policy ${readable(data.policy_revision)}. Each record is one request the daemon authorized for a remote provider: hashes and receipt ids only, never content. A record left pending means the host never reported the outcome.`, "integrity"), items.length
            ? table(items, [
                "when",
                "status",
                "provider",
                "model",
                "host",
                "classification",
                "bytes",
                "receipts",
                "tokens",
                "permit",
            ])
            : e("p", "Nothing has left this store for a provider in the retention window."));
        this.output.append(section);
    }
    async handoffs() {
        this.reader = () => this.handoffs();
        const data = await this.command("inspection", "handoffs");
        if (this.unchanged(data))
            return;
        this.output.replaceChildren(e("h3", "Handoff lifecycle"), e("p", "Only handoffs involving this principal and a currently accessible checkpoint are shown. Role labels grant no authority. Preview uses the sender–recipient permission intersection; this screen never acknowledges work or changes the active writer."), table(rows(data.items), [
            "id",
            "task",
            "checkpoint",
            "sender",
            "recipient",
            "effective",
            "expires",
        ]), detail("Authorized handoff state", data));
        const form = e("form", "", "observatory-form"), handle = field(form, "Handle"), action = e("select");
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
            const args = { handle: handle.value };
            if (action.value !== "cancel")
                Object.assign(args, {
                    max_output_tokens: 8192,
                    tokenizer: "cl100k_base",
                });
            const chosen = action.value;
            preview.replaceChildren(detail("Exact handoff request", { action: chosen, ...args }), button("Confirm " + chosen, () => void this.run(() => this.mutate("handoff", chosen, args))));
        };
        this.output.append(form, preview);
        const prepare = e("details");
        prepare.append(e("summary", "Prepare a handoff from a checkpoint"));
        const f = e("form", "", "observatory-form");
        const fields = Object.fromEntries(["task", "run", "checkpoint", "recipient", "role", "stage"].map((k) => [
            k,
            field(f, k),
        ]));
        const ttl = field(f, "Expires in milliseconds", "60000", "number");
        f.append(e("button", "Preview preparation"));
        const p = e("div");
        f.onsubmit = (ev) => {
            ev.preventDefault();
            const args = Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v.value || null]));
            args.bindings = this.scope();
            args.ttl_ms = Number(ttl.value);
            p.replaceChildren(detail("Exact recipient and scope", args), button("Confirm preparation", () => void this.run(() => this.mutate("handoff", "prepare", args))));
        };
        prepare.append(f, p);
        this.output.append(prepare);
    }
    impact() {
        this.status.textContent =
            "Enter the repository snapshot and paths to check current access.";
        this.output.replaceChildren(e("h3", "Code change impact"), e("p", "Direct evidence links and bounded Python parser relations. Missing files or unsupported languages remain coverage gaps; absence of edges does not prove no impact."));
        const f = e("form", "", "observatory-form"), repository = field(f, "Repository ID"), snapshot = field(f, "Snapshot ID"), paths = field(f, "Changed paths (comma separated)");
        f.append(e("button", "Inspect impact"));
        const result = e("div");
        f.onsubmit = (ev) => {
            ev.preventDefault();
            const args = {
                repository: repository.value,
                snapshot: snapshot.value,
                paths: paths.value
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
            }, bindings = this.scope();
            const read = async () => {
                const value = await this.command("repository", "impact", {
                    ...args,
                    limit: 128,
                }), coverage = await this.command("repository", "coverage", {
                    ...args,
                    bindings,
                });
                if (this.unchanged(value, coverage))
                    return;
                result.replaceChildren(detail("Evidence and affected claims", value), detail("Parser and policy coverage", coverage));
                const host = e("div");
                result.prepend(host);
                this.graph?.clear();
                this.graph = new AtlasView(host, () => { });
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
                    coverage: "Direct current evidence dependencies; no similarity inference",
                    truncation: value.truncated ? ["impact_limit"] : [],
                });
            };
            this.reader = read;
            void this.run(read, true);
        };
        this.output.append(f, result);
    }
    async timeline(before) {
        const project = this.project.value;
        const data = await this.api("/api/timeline/" +
            encodeURIComponent(project) +
            (before === undefined ? "" : "/" + String(before)));
        const items = (data.items ?? []);
        const rows = items.map((item) => ({
            position: item.position,
            when: new Date(Number(item.at_ms)).toLocaleString(),
            event: item.kind,
            subject: `${String(item.subject_kind)} ${String(item.subject)}`,
            actor: item.actor,
        }));
        const body = [
            e("h3", "Timeline"),
            e("p", "Every recorded event whose subject belongs to this project, newest first: captures, claims, reviews, checkpoints, handoffs and purges, with the principal that caused each.", "integrity"),
            rows.length
                ? table(rows, ["position", "when", "event", "subject", "actor"])
                : e("p", "No events recorded for this project yet."),
        ];
        if (before === undefined)
            this.output.replaceChildren(...body);
        else {
            this.output.querySelector("[data-earlier]")?.remove();
            this.output.append(...body.slice(2));
        }
        if (data.next_before !== null && data.next_before !== undefined) {
            const more = e("button", "Load earlier");
            more.type = "button";
            more.dataset.earlier = "1";
            more.onclick = () => void this.run(() => this.timeline(Number(data.next_before)));
            this.output.append(more);
        }
    }
    brief() {
        this.status.textContent =
            "Run the same briefing an agent would receive, with its receipt.";
        this.output.replaceChildren(e("h3", "Ask for a briefing"), e("p", "The query is answered under your current permissions and recorded with a receipt exactly as for an agent. Constraints come first, then cited guidance; sources are evidence, not instructions.", "integrity"));
        const f = e("form", "", "observatory-form");
        const query = field(f, "Query");
        query.required = true;
        query.maxLength = 2048;
        const budget = field(f, "Token budget", "6000", "number");
        budget.min = "256";
        budget.max = "100000";
        const asOf = field(f, "As of (optional)", "", "datetime-local");
        asOf.title =
            "Brief as the project stood at this moment: only what was recorded by then, with claim validity judged then.";
        f.append(e("button", "Brief me"));
        const result = e("div", "", "briefing");
        f.onsubmit = (event) => {
            event.preventDefault();
            void this.run(async () => {
                result.replaceChildren(e("p", "Checking current permission…"));
                const instant = asOf.value ? new Date(asOf.value).getTime() : NaN;
                const packet = await this.command("retrieval", "brief", {
                    query: query.value,
                    bindings: [{}],
                    max_output_tokens: Number(budget.value) || 6000,
                    tokenizer: "utf8-byte-estimate-v1",
                    format: "text",
                    ...(Number.isFinite(instant) ? { as_of_ms: instant } : {}),
                });
                result.replaceChildren(...markdown(String(packet.text ?? "")));
                const usage = (packet.usage ?? {});
                result.append(e("p", `Receipt ${String(packet.receipt)} · status ${String(packet.status)} · ${String(usage.output_tokens ?? "?")} tokens (${String(usage.counting ?? "estimate")})`, "caption"));
                const sources = (packet.sources ?? []);
                if (sources.length) {
                    result.append(detail("Citations", sources.map((s) => `[${String(s.ref)}] ${String(s.kind)} ${String(s.id)}`)));
                }
                const replay = e("button", "Verify replay");
                replay.type = "button";
                replay.onclick = () => void this.run(async () => {
                    const again = await this.command("retrieval", "replay", {
                        receipt: packet.receipt,
                    });
                    replay.textContent =
                        again.text === packet.text
                            ? "Replay matched byte for byte"
                            : "Replay differed";
                });
                result.append(replay);
            });
        };
        this.output.append(f, result);
    }
    influence() {
        this.status.textContent =
            "Enter an observation to list every record built from it and who received them.";
        this.output.replaceChildren(e("h3", "Where did this memory go"), e("p", "The claims that cite the observation, the checkpoints, handoffs, summaries and experience records built on it or on anything derived from it, and the retained briefings, fetches, reruns and graph pages that included it, with the principals they reached. The same recorded dependencies a purge would follow, read without changing anything. Prior exports, provider deliveries and backups leave no record here."));
        const f = e("form", "", "observatory-form"), observation = field(f, "Observation ID");
        observation.required = true;
        f.append(e("button", "Trace"));
        const result = e("div");
        observation.oninput = () => result.replaceChildren();
        f.onsubmit = (ev) => {
            ev.preventDefault();
            const id = observation.value;
            const read = async () => {
                const value = await this.command("inspection", "influence", {
                    observation: id,
                });
                if (this.unchanged(value))
                    return;
                const counts = doc(value.counts);
                result.replaceChildren(e("p", `Captured by ${String(value.captured_by)} from ${String(value.source)} · reached ${String(counts.principals)} principal(s): ${value.principals.join(", ") || "none"}`, "eyebrow"));
                for (const [title, key] of [
                    ["Claims citing it", "claims"],
                    ["Checkpoints built on it", "checkpoints"],
                    ["Handoffs that carried it", "handoffs"],
                    ["Summaries derived from it", "summaries"],
                    ["Deliveries that included it", "deliveries"],
                    ["Experience records", "experience"],
                ]) {
                    const items = rows(value[key]);
                    const truncated = value.truncation.includes(key)
                        ? " (truncated)"
                        : "";
                    result.append(detail(`${title}: ${items.length}${truncated}`, items));
                }
                result.append(e("p", readable(value.coverage), "caption"));
            };
            this.reader = read;
            void this.run(read, true);
        };
        this.output.append(f, result);
    }
    retention() {
        this.status.textContent =
            "Enter an observation to authorize and preview its consequences.";
        this.output.replaceChildren(e("h3", "Retention preview"), e("p", "Permanent purge erases the source and the claims, briefings, checkpoints and handoffs that recorded a dependency on it, deletes receipts that list it, retires replay results tenant-wide, and disables ranking when a dependent experience record is retired. It cannot erase prior exports, provider deliveries or backups. Review the server preview before confirming.", "integrity"));
        const f = e("form", "", "observatory-form"), observation = field(f, "Observation ID");
        observation.required = true;
        f.append(e("button", "Preview purge"));
        const preview = e("div");
        observation.oninput = () => preview.replaceChildren();
        f.onsubmit = (ev) => {
            ev.preventDefault();
            void this.run(async () => {
                const id = observation.value, value = await this.command("inspection", "retention_preview", {
                    observation: id,
                });
                preview.replaceChildren(detail("Affected records, gaps and irreversible effects", Object.fromEntries(Object.entries(value).filter(([key]) => key !== "preview"))));
                const confirmForm = e("form"), confirm = field(confirmForm, `Type ${id} to confirm permanent purge`);
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
