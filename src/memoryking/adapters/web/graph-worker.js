import { layout } from "./graph-layout.js";
const worker = self;
worker.onmessage = (e) => {
    try {
        worker.postMessage({ ok: true, ...layout(e.data.nodes, e.data.edges) });
    }
    catch {
        worker.postMessage({ ok: false });
    }
};
