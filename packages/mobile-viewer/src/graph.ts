import type { ViewerGraphData } from "@memoryking/contracts";

// Bounded, deterministic force relaxation on demand. No worker, telemetry,
// external asset, animated loop or extra data fetch; the semantic list is primary.
export function drawGraph(
  container: HTMLElement,
  graph: ViewerGraphData,
): void {
  const nodes = graph.nodes.slice(0, 100).map((node, i, all) => ({
    ...node,
    x: 160 + 100 * Math.cos((i * 2 * Math.PI) / Math.max(1, all.length)),
    y: 140 + 100 * Math.sin((i * 2 * Math.PI) / Math.max(1, all.length)),
  }));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = graph.edges
    .slice(0, 150)
    .map((edge) => [byId.get(edge.source), byId.get(edge.target)])
    .filter((edge) => edge[0] && edge[1]);
  for (let step = 0; step < 80; step++) {
    const moves = nodes.map(() => ({ x: 0, y: 0 }));
    nodes.forEach((a, i) => {
      nodes.forEach((b, j) => {
        if (i === j) return;
        const dx = a.x - b.x,
          dy = a.y - b.y,
          distance = Math.max(25, dx * dx + dy * dy);
        moves[i].x += (dx * 80) / distance;
        moves[i].y += (dy * 80) / distance;
      });
    });
    for (const [a, b] of edges) {
      if (!a || !b) continue;
      const dx = b.x - a.x,
        dy = b.y - a.y;
      const i = nodes.indexOf(a),
        j = nodes.indexOf(b);
      moves[i].x += dx * 0.018;
      moves[i].y += dy * 0.018;
      moves[j].x -= dx * 0.018;
      moves[j].y -= dy * 0.018;
    }
    nodes.forEach((node, i) => {
      node.x = Math.max(
        16,
        Math.min(304, node.x + Math.max(-5, Math.min(5, moves[i].x))),
      );
      node.y = Math.max(
        16,
        Math.min(264, node.y + Math.max(-5, Math.min(5, moves[i].y))),
      );
    });
  }
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 320 280");
  for (const [a, b] of edges) {
    if (!a || !b) continue;
    const line = document.createElementNS(ns, "line");
    for (const [key, value] of Object.entries({
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
    }))
      line.setAttribute(key, String(value));
    svg.append(line);
  }
  for (const node of nodes) {
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", String(node.x));
    dot.setAttribute("cy", String(node.y));
    dot.setAttribute("r", node.kind === "task" ? "7" : "4");
    dot.setAttribute("class", node.kind);
    const title = document.createElementNS(ns, "title");
    title.textContent = node.label;
    dot.append(title);
    svg.append(dot);
  }
  container.replaceChildren(svg);
}
