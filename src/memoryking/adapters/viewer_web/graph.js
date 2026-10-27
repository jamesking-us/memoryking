export const nodeNames = {
    observation: "Memory",
    claim: "Accepted claim",
    task: "Task",
    criterion: "Criterion",
    plan: "Plan",
    step: "Plan step",
};
const W = 640, H = 440;
const clamp = (n, low, high) => Math.max(low, Math.min(high, n));
// Finite deterministic relaxation. No simulation timer, network or stored layout.
export function layoutGraph(graph) {
    const nodes = graph.nodes.slice(0, 100).map((node, i, all) => ({
        ...node,
        x: W / 2 + 220 * Math.cos((i * 2 * Math.PI) / Math.max(1, all.length)),
        y: H / 2 + 160 * Math.sin((i * 2 * Math.PI) / Math.max(1, all.length)),
    }));
    const byId = new Map(nodes.map((n, i) => [n.id, i]));
    for (let step = 0; step < 100; step++) {
        const moves = nodes.map(() => ({ x: 0, y: 0 }));
        nodes.forEach((a, i) => nodes.forEach((b, j) => {
            if (i >= j)
                return;
            const dx = a.x - b.x, dy = a.y - b.y, d2 = Math.max(64, dx * dx + dy * dy);
            const distance = Math.sqrt(d2);
            const force = 20000 / (d2 * distance) +
                (Math.max(0, 90 - distance) * 0.15) / distance;
            moves[i].x += dx * force;
            moves[i].y += dy * force;
            moves[j].x -= dx * force;
            moves[j].y -= dy * force;
        }));
        for (const edge of graph.edges.slice(0, 150)) {
            const i = byId.get(edge.source), j = byId.get(edge.target);
            if (i === undefined || j === undefined)
                continue;
            const a = nodes[i], b = nodes[j], dx = b.x - a.x, dy = b.y - a.y;
            const distance = Math.max(1, Math.hypot(dx, dy));
            const force = ((distance - 85) * 0.035) / distance;
            moves[i].x += dx * force;
            moves[i].y += dy * force;
            moves[j].x -= dx * force;
            moves[j].y -= dy * force;
        }
        nodes.forEach((node, i) => {
            node.x = clamp(node.x + clamp(moves[i].x, -5, 5), 40, W - 40);
            node.y = clamp(node.y + clamp(moves[i].y, -5, 5), 40, H - 40);
        });
    }
    return nodes;
}
export function drawGraph(container, graph, allowed, open) {
    const el = (tag, text = "", cls = "") => {
        const item = document.createElement(tag);
        item.textContent = text;
        item.className = cls;
        return item;
    };
    const button = (text, action) => {
        const b = el("button", text, "button");
        b.type = "button";
        b.addEventListener("click", () => {
            if (allowed())
                action();
        });
        return b;
    };
    const nodes = layoutGraph(graph), byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = graph.edges
        .slice(0, 150)
        .filter((e) => byId.has(e.source) && byId.has(e.target));
    let scale = 1, pan = { x: 0, y: 0 }, selected = null;
    const toolbar = el("div", "", "toolbar graph-controls");
    const zoom = el("output", "100%");
    zoom.setAttribute("aria-label", "Graph zoom");
    const viewport = el("div", "", "graph-viewport");
    viewport.setAttribute("aria-label", "Interactive knowledge graph");
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("aria-hidden", "true");
    const lines = edges.map((e) => {
        const line = document.createElementNS(ns, "line");
        line.setAttribute("class", e.kind);
        svg.append(line);
        return line;
    });
    viewport.append(svg);
    const buttons = nodes.map((node) => {
        const b = el("button", {
            observation: "M",
            claim: "A",
            task: "T",
            criterion: "C",
            plan: "P",
            step: "S",
        }[node.kind], `graph-node ${node.kind}`);
        b.type = "button";
        b.dataset.node = node.id;
        b.setAttribute("aria-label", `${nodeNames[node.kind]}: ${node.label}`);
        b.append(el("span", node.label.length > 32 ? node.label.slice(0, 32) + "…" : node.label, "graph-node-label"));
        b.title = node.label;
        b.setAttribute("aria-pressed", "false");
        // Pointer selection is handled below, so dragging never opens a node.
        b.addEventListener("click", (e) => {
            if (e.detail === 0 && allowed())
                choose(node.id);
        });
        viewport.append(b);
        return b;
    });
    const detail = el("section", "", "panel graph-detail");
    detail.setAttribute("aria-label", "Selected node");
    const summary = el("p", "Tap a node to see its connections and open its detail.", "muted");
    const list = el("details", "", "relationships");
    list.append(el("summary", `Node list (${nodes.length})`));
    const filterLabel = el("label", "Find in this view");
    const search = el("input");
    search.type = "search";
    search.maxLength = 160;
    search.autocomplete = "off";
    filterLabel.append(search);
    list.append(filterLabel);
    const items = el("ul");
    list.append(items);
    const drawList = () => {
        items.replaceChildren();
        for (const node of nodes.filter((n) => n.label.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()))) {
            const li = el("li");
            li.append(button(`${nodeNames[node.kind]}: ${node.label}`, () => choose(node.id)));
            items.append(li);
        }
        if (!items.children.length)
            items.append(el("li", "No matching nodes in this view."));
    };
    search.addEventListener("input", () => {
        if (allowed())
            drawList();
    });
    drawList();
    function choose(id) {
        if (!allowed())
            return;
        const node = byId.get(id);
        if (!node)
            return;
        selected = id;
        detail.replaceChildren(el("h2", nodeNames[node.kind]), el("p", node.label));
        detail.append(button(node.kind === "plan" || node.kind === "step"
            ? "Open plan"
            : node.kind === "task" || node.kind === "criterion"
                ? "Open task"
                : "Open memory", () => open(id)));
        const connections = edges.filter((e) => e.source === id || e.target === id);
        if (!connections.length)
            detail.append(el("p", "No recorded connections in this bounded view.", "muted"));
        else {
            const links = el("ul");
            for (const edge of connections) {
                const outgoing = edge.source === id, other = byId.get(outgoing ? edge.target : edge.source);
                const relation = outgoing
                    ? edge.kind
                    : {
                        declares: "declared by",
                        uses: "used by",
                        cites: "cited by",
                        depends_on: "prerequisite for",
                    }[edge.kind];
                const li = el("li");
                li.append(button(`${relation}: ${other.label}`, () => choose(other.id)));
                links.append(li);
            }
            detail.append(el("h3", "Recorded connections"), links);
        }
        paint();
    }
    const project = (p) => ({
        x: (p.x - W / 2) * scale + W / 2 + pan.x,
        y: (p.y - H / 2) * scale + H / 2 + pan.y,
    });
    function paint() {
        if (!allowed())
            return;
        zoom.value = `${Math.round(scale * 100)}%`;
        viewport.dataset.zoom = String(scale);
        edges.forEach((e, i) => {
            const a = project(byId.get(e.source)), b = project(byId.get(e.target));
            for (const [key, value] of Object.entries({
                x1: a.x,
                y1: a.y,
                x2: b.x,
                y2: b.y,
            }))
                lines[i].setAttribute(key, String(value));
            lines[i].classList.toggle("selected", e.source === selected || e.target === selected);
        });
        nodes.forEach((n, i) => {
            const p = project(n), b = buttons[i];
            b.style.left = `${(100 * p.x) / W}%`;
            b.style.top = `${(100 * p.y) / H}%`;
            b.setAttribute("aria-pressed", String(n.id === selected));
        });
    }
    function zoomAt(factor, from, to = from) {
        const next = clamp(scale * factor, 0.65, 4), ratio = next / scale;
        pan = {
            x: clamp(to.x - W / 2 - (from.x - W / 2 - pan.x) * ratio, -W * 4, W * 4),
            y: clamp(to.y - H / 2 - (from.y - H / 2 - pan.y) * ratio, -H * 4, H * 4),
        };
        scale = next;
        paint();
    }
    toolbar.append(button("Zoom in", () => zoomAt(1.3, { x: W / 2, y: H / 2 })), button("Zoom out", () => zoomAt(1 / 1.3, { x: W / 2, y: H / 2 })), button("Reset view", () => {
        scale = 1;
        pan = { x: 0, y: 0 };
        const fresh = layoutGraph(graph);
        nodes.forEach((n, i) => {
            n.x = fresh[i].x;
            n.y = fresh[i].y;
        });
        paint();
    }), zoom);
    const labels = button("Labels", () => {
        const shown = viewport.classList.toggle("labels-visible");
        labels.setAttribute("aria-pressed", String(shown));
    });
    const showLabels = false;
    viewport.classList.toggle("labels-visible", showLabels);
    labels.setAttribute("aria-pressed", String(showLabels));
    toolbar.append(labels);
    for (const b of toolbar.querySelectorAll("button")) {
        if (b.textContent === "Zoom in" || b.textContent === "Zoom out") {
            b.setAttribute("aria-label", b.textContent);
            b.textContent = b.textContent === "Zoom in" ? "+" : "−";
        }
    }
    const legend = el("div", "", "graph-legend");
    for (const [kind, name] of Object.entries(nodeNames))
        legend.append(el("span", name, kind));
    const pointers = new Map();
    let drag = null;
    const point = (e) => {
        const r = viewport.getBoundingClientRect();
        return {
            x: ((e.clientX - r.left) * W) / r.width,
            y: ((e.clientY - r.top) * H) / r.height,
        };
    };
    viewport.addEventListener("pointerdown", (e) => {
        if (!allowed() || e.button !== 0)
            return;
        const p = point(e);
        pointers.set(e.pointerId, p);
        viewport.setPointerCapture(e.pointerId);
        const target = e.target instanceof Element
            ? e.target.closest(".graph-node")
            : null;
        drag =
            pointers.size === 1
                ? { id: target?.dataset.node ?? null, start: p, last: p, moved: false }
                : null;
        e.preventDefault();
    });
    viewport.addEventListener("pointermove", (e) => {
        if (!pointers.has(e.pointerId) || !allowed())
            return;
        const old = [...pointers.values()];
        pointers.set(e.pointerId, point(e));
        const next = [...pointers.values()];
        if (next.length === 2) {
            const mid = (a, b) => ({
                x: (a.x + b.x) / 2,
                y: (a.y + b.y) / 2,
            });
            zoomAt(Math.hypot(next[1].x - next[0].x, next[1].y - next[0].y) /
                Math.max(1, Math.hypot(old[1].x - old[0].x, old[1].y - old[0].y)), mid(old[0], old[1]), mid(next[0], next[1]));
        }
        else if (next.length === 1 && drag) {
            const p = next[0], dx = p.x - drag.last.x, dy = p.y - drag.last.y;
            drag.moved ||= Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 5;
            if (drag.moved) {
                const node = drag.id ? byId.get(drag.id) : null;
                if (node) {
                    node.x = clamp(node.x + dx / scale, -W, W * 2);
                    node.y = clamp(node.y + dy / scale, -H, H * 2);
                }
                else {
                    pan.x = clamp(pan.x + dx, -W * 4, W * 4);
                    pan.y = clamp(pan.y + dy, -H * 4, H * 4);
                }
                paint();
            }
            drag.last = p;
        }
    });
    const release = (e) => {
        if (!pointers.has(e.pointerId))
            return;
        if (e.type === "pointerup" && drag?.id && !drag.moved && allowed())
            choose(drag.id);
        pointers.delete(e.pointerId);
        drag = null;
        if (viewport.hasPointerCapture(e.pointerId))
            viewport.releasePointerCapture(e.pointerId);
    };
    viewport.addEventListener("pointerup", release);
    viewport.addEventListener("pointercancel", release);
    container.replaceChildren(toolbar, el("p", "Drag nodes or the background. Pinch to zoom or use the buttons.", "muted"), viewport, legend, el("p", "Recorded links only. Distance is not confidence.", "muted"), summary, detail, list);
    paint();
}
