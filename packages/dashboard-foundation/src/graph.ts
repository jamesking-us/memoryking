import type { LayoutNode } from "./graph-layout.js";
export interface GraphNode extends LayoutNode {
  type: string;
  label: string;
  authority?: string;
  [key: string]: unknown;
}
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  provenance: unknown;
}
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncation?: string[];
  coverage?: string;
  next_cursor?: string | null;
  [key: string]: unknown;
}
export interface Citation {
  receipt: string;
  status: string;
  created_ms: number;
  dependency_kind: string;
  format?: string;
  usage?: { output_tokens?: number; counting?: string };
}
/** Receipt trail for a node: load the caller's receipts that used it, open one. */
export interface ReceiptTrail {
  load: (id: string) => Promise<Citation[]>;
  open: (receipt: string) => void;
}
export type Authority =
  | "observation"
  | "accepted"
  | "proposed"
  | "quarantined"
  | "retired"
  | "constraint"
  | "attestation"
  | "procedure"
  | "working";
/** Legend order and wording; colours live in app.css under [data-authority]. */
export const AUTHORITY_LEGEND: ReadonlyArray<[Authority, string]> = [
  ["observation", "Observation: recorded, not reviewed"],
  ["accepted", "Accepted claim"],
  ["proposed", "Proposed claim: awaiting review"],
  ["quarantined", "Quarantined claim"],
  ["retired", "Rejected or deprecated claim"],
  ["constraint", "Mandatory constraint"],
  ["attestation", "Attestation by an actor"],
  ["procedure", "Procedure"],
  ["working", "Checkpoint, task or run"],
];
/** Authority is what the store recorded about the node; layout never changes it. */
export function authorityOf(node: {
  type: string;
  authority?: string;
  state?: unknown;
  quarantined?: unknown;
}): Authority {
  switch (node.type) {
    case "observation":
      return "observation";
    case "attestation":
      return "attestation";
    case "procedure":
      return "procedure";
    case "checkpoint":
    case "task":
    case "run":
      return "working";
    case "claim":
      if (node.quarantined === true) return "quarantined";
      if (node.authority === "mandatory_constraint") return "constraint";
      if (node.state === "accepted") return "accepted";
      if (node.state === "proposed") return "proposed";
      return "retired";
    default:
      return "working";
  }
}
/** Radius grows with the square root of citations and is capped; selection adds a ring. */
export function nodeRadius(citations: unknown, selected = false): number {
  const count =
    typeof citations === "number" && Number.isFinite(citations) && citations > 0
      ? citations
      : 0;
  return Math.min(16, 8 + 2 * Math.sqrt(count)) + (selected ? 3 : 0);
}
const ns = "http://www.w3.org/2000/svg";
function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
): SVGElementTagNameMap[K] {
  const e = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
function html(tag: string, text = ""): HTMLElement {
  const e = document.createElement(tag);
  e.textContent = text;
  return e;
}
export class AtlasView {
  private worker: Worker | null = null;
  private positions = new Map<string, LayoutNode>();
  private selected: string | null = null;
  private data: GraphData = { nodes: [], edges: [] };
  private graph = svg("svg", {
    viewBox: "0 0 900 480",
    role: "group",
    "aria-label":
      "Memory relationships. Use node buttons or the equivalent list.",
  });
  private list = html("div");
  private inspector = html("section");
  private status = html("p");
  private search = document.createElement("input");
  private type = document.createElement("select");
  private ticket = 0;
  private neighbors = false;
  constructor(
    private root: HTMLElement,
    private expand: (id: string) => void,
    private receipts: ReceiptTrail | null = null,
  ) {
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
      ["Fit all", () => this.fit()],
      [
        "Fit selection",
        () => {
          const p = this.positions.get(this.selected ?? "");
          if (p?.x !== undefined && p.y !== undefined)
            this.graph.setAttribute(
              "viewBox",
              `${p.x - 150} ${p.y - 120} 300 240`,
            );
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
          if (this.selected) this.expand(this.selected);
        },
      ],
    ] as Array<[string, () => void]>) {
      const b = html("button", name);
      b.onclick = action;
      controls.append(b);
    }
    const drawing = html("button", "Show drawing");
    drawing.className = "graph-drawing-toggle";
    drawing.setAttribute("aria-pressed", "false");
    drawing.onclick = () => {
      const shown = this.root.classList.toggle("graph-shown");
      drawing.textContent = shown ? "List only" : "Show drawing";
      drawing.setAttribute("aria-pressed", String(shown));
    };
    controls.append(drawing);
    const legend = html("ul");
    legend.className = "graph-legend";
    legend.setAttribute("aria-label", "Node colours by authority");
    for (const [authority, text] of AUTHORITY_LEGEND) {
      const item = html("li", text);
      item.dataset.authority = authority;
      legend.append(item);
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
    root.append(
      controls,
      legend,
      this.status,
      pane,
      html(
        "p",
        "Colour is the recorded authority and size is how many of your receipts cited the record. Layout distance is not confidence. Lines describe recorded relations within the loaded, authorized window. Dragging changes presentation only. On a narrow screen the list stands in for the drawing.",
      ),
    );
  }
  clear(): void {
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
  set(data: GraphData): void {
    this.freeze();
    this.data = {
      ...data,
      nodes: data.nodes.slice(0, 500),
      edges: data.edges.slice(0, 1500),
    };
    const ids = new Set(this.data.nodes.map((n) => n.id));
    for (const key of this.positions.keys())
      if (!ids.has(key)) this.positions.delete(key);
    this.data.nodes.forEach((n, i) => {
      if (!this.positions.has(n.id))
        this.positions.set(n.id, {
          id: n.id,
          x: 450 + 200 * Math.cos(i * 2.399),
          y: 240 + 170 * Math.sin(i * 2.399),
        });
    });
    if (this.selected && !ids.has(this.selected)) this.selected = null;
    this.inspector.replaceChildren();
    this.draw();
    this.settle();
  }
  freeze(): void {
    this.worker?.terminate();
    this.worker = null;
    this.ticket++;
  }
  /** Frame the drawn nodes with room for labels; an empty graph keeps the full frame. */
  private fit(): void {
    const shown = this.data.nodes
      .map((n) => this.positions.get(n.id))
      .filter((p): p is LayoutNode => p?.x !== undefined && p.y !== undefined);
    if (!shown.length) {
      this.graph.setAttribute("viewBox", "0 0 900 480");
      return;
    }
    const xs = shown.map((p) => p.x as number),
      ys = shown.map((p) => p.y as number);
    const minX = Math.min(...xs) - 40,
      maxX = Math.max(...xs) + 160,
      minY = Math.min(...ys) - 40,
      maxY = Math.max(...ys) + 40;
    const width = Math.max(320, maxX - minX),
      height = Math.max(200, maxY - minY);
    this.graph.setAttribute(
      "viewBox",
      `${Math.round(minX)} ${Math.round(minY)} ${Math.round(width)} ${Math.round(height)}`,
    );
  }
  private settle(): void {
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
      worker.onmessage = (
        e: MessageEvent<{
          ok: boolean;
          nodes: LayoutNode[];
          elapsed_ms: number;
          iterations: number;
        }>,
      ) => {
        window.clearTimeout(timeout);
        if (ticket !== this.ticket) return;
        worker.terminate();
        this.worker = null;
        if (e.data.ok) {
          for (const p of e.data.nodes) this.positions.set(p.id, p);
          this.draw();
          this.fit();
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
    } catch {
      this.status.textContent = "Static graph; worker unavailable.";
    }
  }
  private pin(): void {
    const p = this.positions.get(this.selected ?? "");
    if (p) {
      p.pinned = !p.pinned;
      this.inspect(p.id);
      this.draw();
    }
  }
  private inspect(id: string): void {
    this.selected = id;
    const node = this.data.nodes.find((n) => n.id === id),
      edge = this.data.edges.find((e) => e.id === id);
    const value = node ?? edge;
    const pre = html("pre", JSON.stringify(value, null, 2));
    this.inspector.replaceChildren(
      html("h3", node?.label.slice(0, 100) ?? edge?.type ?? "Selection"),
      html(
        "p",
        node
          ? `${node.type} · ${authorityOf(node).replaceAll("-", " ")} (${node.authority ?? "recorded"}) · ${this.positions.get(id)?.pinned ? "pinned" : "unpinned"}`
          : "Typed directional edge; provenance below.",
      ),
    );
    if (node) this.inspector.append(this.trail(node));
    this.inspector.append(pre);
  }
  /** Receipts are the evidence that a record was actually used; they open in Retrieval history. */
  private trail(node: GraphNode): HTMLElement {
    const box = html("div");
    box.className = "graph-trail";
    const count =
      typeof node.citations === "number" ? (node.citations as number) : 0;
    box.append(
      html(
        "p",
        count === 1
          ? "Cited by 1 of your retained receipts."
          : `Cited by ${count} of your retained receipts.`,
      ),
    );
    if (!this.receipts || count === 0) return box;
    const show = document.createElement("button");
    show.textContent = "Show receipts";
    show.className = "graph-trail-load";
    show.onclick = () => {
      show.disabled = true;
      void this.receipts!.load(node.id).then(
        (items) => {
          show.remove();
          const list = html("div");
          list.className = "graph-trail-list";
          for (const item of items) {
            const open = html(
              "button",
              `${item.status.replaceAll("_", " ")} · ${item.usage?.output_tokens ?? "?"} tokens · ${new Date(item.created_ms).toLocaleString()} · via ${item.dependency_kind.replaceAll("_", " ")}`,
            );
            open.className = "graph-trail-item";
            open.dataset.receipt = item.receipt;
            open.onclick = () => this.receipts!.open(item.receipt);
            list.append(open);
          }
          if (!items.length)
            list.append(
              html(
                "p",
                "No receipt is currently available: it expired, or its evidence is no longer readable.",
              ),
            );
          box.append(list);
        },
        () => {
          show.disabled = false;
          box.append(
            html("p", "Receipts unavailable under the current session."),
          );
        },
      );
    };
    box.append(show);
    return box;
  }
  private draw(): void {
    const drawStarted = performance.now();
    const focused = (
      document.activeElement as HTMLElement | null
    )?.getAttribute("data-focus-key");
    const needle = this.search.value.toLowerCase();
    let nodes = this.data.nodes.filter(
      (n) =>
        (this.type.value === "all" || n.type === this.type.value) &&
        n.label.toLowerCase().includes(needle),
    );
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
    const edges = this.data.edges.filter(
      (e) => ids.has(e.source) && ids.has(e.target),
    );
    this.graph.replaceChildren();
    this.list.replaceChildren();
    const defs = svg("defs"),
      marker = svg("marker", {
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
      const a = this.positions.get(e.source)!,
        b = this.positions.get(e.target)!;
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
      const p = this.positions.get(n.id)!;
      const authority = authorityOf(n);
      const g = svg("g", {
        transform: `translate(${p.x},${p.y})`,
        class: "atlas-node " + n.type,
        "data-authority": authority,
        tabindex: "0",
        role: "button",
        "aria-label": `${n.type}, ${authority}: ${n.label}. Cited by ${String(n.citations ?? 0)} receipts. ${p.pinned ? "Pinned." : ""}`,
      });
      g.append(
        svg("circle", {
          r: String(nodeRadius(n.citations, n.id === this.selected)),
        }),
      );
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
        if (
          ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)
        ) {
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
            .querySelector<SVGGElement>(
              `[data-node-index="${nodes.indexOf(n)}"]`,
            )
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
        if (!g.hasPointerCapture(ev.pointerId)) return;
        const matrix = this.graph.getScreenCTM();
        if (!matrix) return;
        const point = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(
          matrix.inverse(),
        );
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
      const button = html(
        "button",
        `${n.type} · ${authority} · ${n.label.slice(0, 100)}`,
      );
      button.dataset.authority = authority;
      button.setAttribute("data-focus-key", "list:" + n.id);
      button.onclick = () => {
        this.inspect(n.id);
        this.draw();
      };
      this.list.append(button);
    }
    if (focused)
      Array.from(this.root.querySelectorAll<HTMLElement>("[data-focus-key]"))
        .find((n) => n.getAttribute("data-focus-key") === focused)
        ?.focus({ preventScroll: true });
    this.root.dataset.drawMs = String(performance.now() - drawStarted);
    this.status.textContent = `${nodes.length} nodes / ${edges.length} edges shown from ${this.data.nodes.length} / ${this.data.edges.length} loaded. ${this.data.coverage ?? "Bounded authorized data"}. Limits: ${(this.data.truncation ?? []).join(", ") || "loaded window only"}.`;
  }
}
