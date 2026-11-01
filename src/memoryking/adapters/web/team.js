const FAMILIES = [
    "captures",
    "claims",
    "checkpoints",
    "handoffs",
    "summaries",
    "dispatches",
    "purges",
    "policy",
    "procedures",
    "other",
];
function el(tag, text = "", cls = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    if (cls)
        node.className = cls;
    return node;
}
export class Team {
    root;
    host;
    project = "";
    constructor(root, host) {
        this.root = root;
        this.host = host;
    }
    clear() {
        this.root.replaceChildren();
        this.project = "";
    }
    async load(project) {
        this.project = project;
        this.root.replaceChildren(el("p", "Checking current access…", "caption"));
        let activity;
        try {
            activity = await this.host.api("/api/activity/" + encodeURIComponent(project));
        }
        catch (error) {
            this.root.replaceChildren(el("p", "Team view unavailable: " +
                (error instanceof Error
                    ? error.message.replaceAll("_", " ")
                    : "request failed"), "caption"));
            return;
        }
        if (this.project !== project)
            return;
        const principals = (activity.principals ?? []);
        const since = new Date(Number(activity.since_ms)).toLocaleDateString();
        const head = el("div");
        head.append(el("h3", "Who did what"), el("p", `${principals.length} principal${principals.length === 1 ? "" : "s"} with grants or records since ${since} (${String(activity.days)} days)${activity.complete ? "" : " · the oldest events were not scanned"}. Counts are of recorded events, briefings and delivered provider dispatches; they describe activity, not quality.`, "integrity"));
        this.root.replaceChildren(head);
        if (principals.length) {
            const wrap = el("div", "", "table-scroll");
            const table = el("table");
            const headRow = el("tr");
            for (const name of [
                "principal",
                "grants",
                ...FAMILIES,
                "briefings",
                "delivered",
                "last seen",
                "",
            ])
                headRow.append(el("th", name));
            table.append(headRow);
            for (const item of principals) {
                const row = el("tr");
                row.append(el("td", item.principal), el("td", item.grants.includes("admin")
                    ? "admin (all)"
                    : item.grants.length
                        ? item.grants.join(", ")
                        : "none now"));
                for (const family of FAMILIES)
                    row.append(el("td", String(item.events[family] ?? 0)));
                row.append(el("td", String(item.briefings)), el("td", String(item.dispatches_delivered)), el("td", item.last_seen_ms
                    ? new Date(item.last_seen_ms).toLocaleString()
                    : "never"));
                const actions = el("td");
                if (item.grants.length) {
                    const revoke = el("button", "Revoke");
                    revoke.type = "button";
                    revoke.className = "quiet";
                    revoke.onclick = () => void this.revoke(item.principal, revoke);
                    actions.append(revoke);
                }
                row.append(actions);
                table.append(row);
            }
            wrap.append(table);
            this.root.append(wrap);
        }
        else {
            this.root.append(el("p", "No grants or records for this project."));
        }
        const admin = el("section");
        admin.append(el("h3", "Administration"), el("p", "Revoking removes the principal's grant on this project (or the tenant-wide grant that covers it) and advances the policy epoch, so every signed-in dashboard, including this one, signs in again. Issuing credentials stays on the command line, where the secret is written to a private file instead of a browser.", "caption"));
        const exportButton = el("button", "Export audit (JSON)");
        exportButton.type = "button";
        exportButton.onclick = () => void this.export(exportButton);
        admin.append(exportButton, el("p", "The export holds identities, kinds, hashes and instants for events, provider dispatches, retained receipts, summaries and grants; never observation text or artifact bodies. It is bounded and says so when truncated.", "caption"));
        this.root.append(admin);
    }
    async revoke(principal, button) {
        const csrf = this.host.csrf();
        if (!csrf)
            return;
        if (!window.confirm(`Revoke ${principal}'s grant on ${this.project}? Every dashboard session will need to sign in again.`))
            return;
        button.disabled = true;
        try {
            await this.host.api("/api/admin/revoke", {
                csrf,
                principal,
                project: this.project,
            });
            this.host.signedOut(`Revoked ${principal} on ${this.project}. Permissions changed; sign in again.`);
        }
        catch (error) {
            button.disabled = false;
            this.root.append(el("p", "Revoke failed: " +
                (error instanceof Error
                    ? error.message.replaceAll("_", " ")
                    : "request failed"), "caption"));
        }
    }
    async export(button) {
        button.disabled = true;
        try {
            const data = await this.host.api("/api/export/" + encodeURIComponent(this.project));
            const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
            const a = document.createElement("a");
            a.href = url;
            a.download = `memoryking-audit-${this.project}-${String(data.generated_ms)}.json`;
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 0);
            const truncated = (data.truncated ?? []);
            this.root.append(el("p", `Exported ${String(data.events.length)} events, ${String(data.egress.length)} dispatches, ${String(data.artifacts.length)} receipts, ${String(data.summaries.length)} summaries${truncated.length ? " · truncated: " + truncated.join(", ") : ""}.`, "caption"));
        }
        catch (error) {
            this.root.append(el("p", "Export failed: " +
                (error instanceof Error
                    ? error.message.replaceAll("_", " ")
                    : "request failed"), "caption"));
        }
        finally {
            button.disabled = false;
        }
    }
}
