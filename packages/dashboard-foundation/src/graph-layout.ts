/** Finite, deterministic layout; positions carry no evidentiary meaning. */
export interface LayoutNode {
  id: string;
  x?: number;
  y?: number;
  pinned?: boolean;
}
export interface LayoutEdge {
  source: string;
  target: string;
}
export function layout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  budget = 700,
): {
  nodes: LayoutNode[];
  elapsed_ms: number;
  iterations: number;
  bounded: boolean;
} {
  if (nodes.length > 500 || edges.length > 1500) throw new Error("graph_limit");
  const start = performance.now();
  const points = nodes.map((n, i) => ({
    ...n,
    x: n.x ?? 450 + 220 * Math.cos(i * 2.399963),
    y: n.y ?? 240 + 180 * Math.sin(i * 2.399963),
  }));
  const indices = new Map(points.map((n, i) => [n.id, i]));
  let iteration = 0;
  for (; iteration < 80 && performance.now() - start < budget; iteration++) {
    const forces = points.map(() => [0, 0]);
    for (let i = 0; i < points.length; i++)
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i],
          b = points[j],
          dx = a.x - b.x || 0.01,
          dy = a.y - b.y || 0.01;
        const d2 = Math.max(100, dx * dx + dy * dy),
          power = 140 / Math.sqrt(points.length + 1) / d2;
        forces[i][0] += dx * power;
        forces[i][1] += dy * power;
        forces[j][0] -= dx * power;
        forces[j][1] -= dy * power;
      }
    for (const e of edges) {
      const i = indices.get(e.source),
        j = indices.get(e.target);
      if (i === undefined || j === undefined) continue;
      const dx = points[j].x - points[i].x,
        dy = points[j].y - points[i].y;
      const d = Math.hypot(dx, dy) || 1,
        power = ((d - 65) * 0.055) / d;
      forces[i][0] += dx * power;
      forces[i][1] += dy * power;
      forces[j][0] -= dx * power;
      forces[j][1] -= dy * power;
    }
    const cool = 0.8 * (1 - iteration / 100);
    points.forEach((p, i) => {
      if (!p.pinned) {
        p.x = Math.max(
          35,
          Math.min(
            865,
            p.x +
              Math.max(-12, Math.min(12, forces[i][0] + (450 - p.x) * 0.03)) *
                cool,
          ),
        );
        p.y = Math.max(
          30,
          Math.min(
            450,
            p.y +
              Math.max(-12, Math.min(12, forces[i][1] + (240 - p.y) * 0.03)) *
                cool,
          ),
        );
      }
    });
  }
  return {
    nodes: points,
    elapsed_ms: performance.now() - start,
    iterations: iteration,
    bounded: iteration < 80,
  };
}
