import { layout, type LayoutNode, type LayoutEdge } from "./graph-layout.js";
const worker = self as unknown as {
  onmessage: (e: MessageEvent) => void;
  postMessage: (v: unknown) => void;
};
worker.onmessage = (
  e: MessageEvent<{ nodes: LayoutNode[]; edges: LayoutEdge[] }>,
) => {
  try {
    worker.postMessage({ ok: true, ...layout(e.data.nodes, e.data.edges) });
  } catch {
    worker.postMessage({ ok: false });
  }
};
