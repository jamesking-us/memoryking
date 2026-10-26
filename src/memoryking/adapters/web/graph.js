const ns = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs))
        e.setAttribute(k, v);
    return e;
}
function html(tag, text = "") {
    const e = document.createElement(tag);
    e.textContent = text;
    return e;
}
export class AtlasView {
    root;
    expand;
    worker = null;
    positions = new Map();
    selected = null;
    data = { nodes: [], edges: [] };
    graph = svg("svg", {
        viewBox: "0 0 900 480",
        role: "group",
        "aria-label": "Memory relationships. Use node buttons or the equivalent list.",
    });
    list = html("div");
    inspector = html("section");
    status = html("p");
    search = document.createElement("input");
    type = document.createElement("select");
    ticket = 0;
    neighbors = false;
    constructor(root, expand) {
        this.root = root;
        this.expand = expand;
        this.root.classList.add("atlas-workspace");
        const controls = html("div");
        controls.className = "actions graph-controls";
        this.search.type = "search";
        this.search.placeholder = "Filter loaded labels";
        this.search.setAttribute("aria-label", "Search loaded graph");
        this.type.setAttribute("aria-label", "Node type");
        for (const name of [
            "all",
            "observation",
            "claim",
            "attestation",
            "procedure",
            "checkpoint",
            "task",
            "run",
            "file",
            "symbol",
        ]) {
            const o = document.createElement("option");
            o.value = name;
            o.textContent = name;
            this.type.append(o);
        }
        this.search.oninput = () => this.draw();
        this.type.onchange = () => this.draw();
        controls.append(this.search, this.type);
        for (const [name, action] of [
            ["Re-layout", () => this.settle()],
            ["Freeze layout", () => this.freeze()],
            [
                "Fit all",
                () => {
                    this.graph.setAttribute("viewBox", "0 0 900 480");
                },
            ],
            [
                "Fit selection",
                () => {
                    const p = this.positions.get(this.selected ?? "");
                    if (p?.x !== undefined && p.y !== undefined)
                        this.graph.setAttribute("viewBox", `${p.x - 150} ${p.y - 120} 300 240`);
                },
            ],
            [
                "Focus neighbors",
                () => {
                    this.neighbors = !this.neighbors;
                    this.draw();
                },
            ],
            ["Pin / unpin selection", () => this.pin()],
            [
                "Expand one hop",
                () => {
                    if (this.selected)
                        this.expand(this.selected);
                },
            ],
        ]) {
            const b = html("button", name);
            b.onclick = action;
            controls.append(b);
        }
        this.status.setAttribute("role", "status");
        const pane = html("div");
        pane.className = "graph-panes";
        const details = html("div");
        details.className = "graph-details";
        this.list.className = "graph-list";
        this.list.setAttribute("aria-label", "Equivalent node list");
        this.inspector.setAttribute("aria-label", "Selected node or edge");
        details.append(this.list, this.inspector);
        pane.append(this.graph, details);
        root.append(controls, this.status, pane, html("p", "Layout distance is not confidence. Lines describe recorded relations within the loaded, authorized window. Dragging changes presentation only."));
    }
    clear() {
        this.freeze();
        this.ticket++;
        this.positions.clear();
        this.data = { nodes: [], edges: [] };
        this.selected = null;
        this.graph.replaceChildren();
        this.list.replaceChildren();
        this.inspector.replaceChildren();
        this.status.textContent = "Graph cleared; current permission required.";
    }
    set(data) {
        this.freeze();
        this.data = {
            ...data,
            nodes: data.nodes.slice(0, 500),
            edges: data.edges.slice(0, 1500),
        };
        const ids = new Set(this.data.nodes.map((n) => n.id));
        for (const key of this.positions.keys())
            if (!ids.has(key))
                this.positions.delete(key);
        this.data.nodes.forEach((n, i) => {
            if (!this.positions.has(n.id))
                this.positions.set(n.id, {
                    id: n.id,
                    x: 450 + 200 * Math.cos(i * 2.399),
                    y: 240 + 170 * Math.sin(i * 2.399),
                });
        });
        if (this.selected &&
            !ids.has(this.selected) &&
            !this.data.edges.some((edge) => edge.id === this.selected))
            this.selected = null;
        this.inspector.replaceChildren();
        if (this.selected)
            this.inspect(this.selected);
        this.draw();
        this.settle();
    }
    freeze() {
        this.worker?.terminate();
        this.worker = null;
        this.ticket++;
    }
    settle() {
        this.freeze();
        const ticket = this.ticket;
        this.status.textContent = "Settling a bounded layout…";
        try {
            const worker = new Worker("/graph-worker.js", { type: "module" });
            this.worker = worker;
            const timeout = window.setTimeout(() => {
                if (ticket === this.ticket) {
                    this.freeze();
                    this.status.textContent =
                        "Layout budget reached. Current positions and the list remain usable.";
                }
            }, 1000);
            worker.onmessage = (e) => {
                window.clearTimeout(timeout);
                if (ticket !== this.ticket)
                    return;
                worker.terminate();
                this.worker = null;
                if (e.data.ok) {
                    for (const p of e.data.nodes)
                        this.positions.set(p.id, p);
                    this.draw();
                    this.status.textContent += ` · layout ${e.data.elapsed_ms.toFixed(1)} ms / ${e.data.iterations} iterations`;
                }
            };
            worker.onerror = () => {
                window.clearTimeout(timeout);
                this.freeze();
                this.status.textContent =
                    "Layout worker unavailable; static positions and list remain usable.";
            };
            worker.postMessage({
                nodes: [...this.positions.values()],
                edges: this.data.edges,
            });
        }
        catch {
            this.status.textContent = "Static graph; worker unavailable.";
        }
    }
    pin() {
        const p = this.positions.get(this.selected ?? "");
        if (p) {
            p.pinned = !p.pinned;
            this.inspect(p.id);
            this.draw();
        }
    }
    inspect(id) {
        this.selected = id;
        const node = this.data.nodes.find((n) => n.id === id), edge = this.data.edges.find((e) => e.id === id);
        const value = node ?? edge;
        const pre = html("pre", JSON.stringify(value, null, 2));
        this.inspector.replaceChildren(html("h3", node?.label.slice(0, 100) ?? edge?.type ?? "Selection"), html("p", node
            ? `${node.type} · ${node.authority ?? "recorded relationship"} · ${this.positions.get(id)?.pinned ? "pinned" : "unpinned"}`
            : "Typed directional edge; provenance below."), pre);
    }
    draw() {
        const drawStarted = performance.now();
        const focused = document.activeElement?.getAttribute("data-focus-key");
        const needle = this.search.value.toLowerCase();
        let nodes = this.data.nodes.filter((n) => (this.type.value === "all" || n.type === this.type.value) &&
            n.label.toLowerCase().includes(needle));
        if (this.neighbors && this.selected) {
            const ids = new Set([this.selected]);
            for (const e of this.data.edges)
                if (e.source === this.selected || e.target === this.selected) {
                    ids.add(e.source);
                    ids.add(e.target);
                }
            nodes = nodes.filter((n) => ids.has(n.id));
        }
        const ids = new Set(nodes.map((n) => n.id));
        const edges = this.data.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
        this.graph.replaceChildren();
        this.list.replaceChildren();
        const defs = svg("defs"), marker = svg("marker", {
            id: "relation-arrow",
            viewBox: "0 0 10 10",
            refX: "20",
            refY: "5",
            markerWidth: "5",
            markerHeight: "5",
            orient: "auto-start-reverse",
        });
        marker.append(svg("path", { d: "M0 0L10 5L0 10z", fill: "var(--cyan)" }));
        defs.append(marker);
        this.graph.append(defs);
        for (const e of edges) {
            const a = this.positions.get(e.source), b = this.positions.get(e.target);
            const line = svg("line", {
                x1: String(a.x),
                y1: String(a.y),
                x2: String(b.x),
                y2: String(b.y),
                class: "atlas-edge " + e.type,
                "marker-end": "url(#relation-arrow)",
                tabindex: "0",
                role: "button",
                "aria-label": `${e.type}: ${e.source} to ${e.target}`,
            });
            const title = svg("title");
            title.textContent = e.type;
            line.append(title);
            line.setAttribute("data-focus-key", "edge:" + e.id);
            line.onclick = () => this.inspect(e.id);
            line.onkeydown = (ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                    ev.preventDefault();
                    this.inspect(e.id);
                }
            };
            this.graph.append(line);
        }
        for (const n of nodes) {
            const p = this.positions.get(n.id);
            const g = svg("g", {
                transform: `translate(${p.x},${p.y})`,
                class: "atlas-node " + n.type,
                tabindex: "0",
                role: "button",
                "aria-label": `${n.type}: ${n.label}. ${p.pinned ? "Pinned." : ""}`,
            });
            g.append(svg("circle", { r: n.id === this.selected ? "12" : "9" }));
            const title = svg("title");
            title.textContent = n.label;
            g.append(title);
            if (nodes.length <= 16 || n.id === this.selected) {
                const text = svg("text", { x: "14", y: "5" });
                text.textContent = n.label.slice(0, 28);
                g.append(text);
            }
            g.setAttribute("data-focus-key", "node:" + n.id);
            g.onclick = () => {
                this.inspect(n.id);
                this.draw();
            };
            g.onkeydown = (ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                    ev.preventDefault();
                    this.inspect(n.id);
                }
                if (ev.key.toLowerCase() === "p") {
                    this.selected = n.id;
                    this.pin();
                }
                if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)) {
                    ev.preventDefault();
                    this.freeze();
                    p.pinned = true;
                    p.x =
                        (p.x ?? 450) +
                            (ev.key === "ArrowRight" ? 10 : ev.key === "ArrowLeft" ? -10 : 0);
                    p.y =
                        (p.y ?? 240) +
                            (ev.key === "ArrowDown" ? 10 : ev.key === "ArrowUp" ? -10 : 0);
                    this.draw();
                    this.graph
                        .querySelector(`[data-node-index="${nodes.indexOf(n)}"]`)
                        ?.focus();
                }
            };
            g.setAttribute("data-node-index", String(nodes.indexOf(n)));
            g.onpointerdown = (ev) => {
                this.freeze();
                this.selected = n.id;
                g.setPointerCapture(ev.pointerId);
                p.pinned = true;
            };
            g.onpointermove = (ev) => {
                if (!g.hasPointerCapture(ev.pointerId))
                    return;
                const matrix = this.graph.getScreenCTM();
                if (!matrix)
                    return;
                const point = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(matrix.inverse());
                p.x = point.x;
                p.y = point.y;
                g.setAttribute("transform", `translate(${p.x},${p.y})`);
            };
            g.onpointerup = (ev) => {
                if (g.hasPointerCapture(ev.pointerId)) {
                    g.releasePointerCapture(ev.pointerId);
                    this.inspect(n.id);
                    this.draw();
                }
            };
            this.graph.append(g);
            const button = html("button", `${n.type} · ${n.label.slice(0, 100)}`);
            button.setAttribute("data-focus-key", "list:" + n.id);
            button.onclick = () => {
                this.inspect(n.id);
                this.draw();
            };
            this.list.append(button);
        }
        if (focused)
            Array.from(this.root.querySelectorAll("[data-focus-key]"))
                .find((n) => n.getAttribute("data-focus-key") === focused)
                ?.focus({ preventScroll: true });
        this.root.dataset.drawMs = String(performance.now() - drawStarted);
        this.status.textContent = `${nodes.length} nodes / ${edges.length} edges shown from ${this.data.nodes.length} / ${this.data.edges.length} loaded. ${this.data.coverage ?? "Bounded authorized data"}. Limits: ${(this.data.truncation ?? []).join(", ") || "loaded window only"}.`;
    }
}
