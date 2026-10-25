import assert from "node:assert/strict";
import test from "node:test";
import { layout } from "../../packages/dashboard-foundation/dist/graph-layout.js";
import {
  crownMesh,
  installCrown,
} from "../../packages/dashboard-foundation/dist/brand.js";

test("bounded force graph terminates with finite positions and preserves pinned nodes", () => {
  for (const count of [0, 1, 200, 500]) {
    const nodes = Array.from({ length: count }, (_, i) => ({ id: String(i) }));
    if (count) Object.assign(nodes[0], { x: 123, y: 234, pinned: true });
    const edges = Array.from({ length: Math.min(count * 3, 1500) }, (_, i) => ({
      source: String(i % count),
      target: String((i + 7) % count),
    }));
    const result = layout(nodes, edges);
    assert.ok(result.iterations <= 80);
    assert.equal(result.nodes.length, count);
    assert.ok(
      result.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)),
    );
    if (count) {
      assert.equal(result.nodes[0].x, 123);
      assert.equal(result.nodes[0].y, 234);
    }
  }
});
test("force graph ceiling rejects oversize input; dangling references cannot move hidden endpoints", () => {
  assert.throws(
    () =>
      layout(
        Array.from({ length: 501 }, (_, i) => ({ id: String(i) })),
        [],
      ),
    /graph_limit/,
  );
  assert.throws(
    () => layout([], Array(1501).fill({ source: "a", target: "b" })),
    /graph_limit/,
  );
  const a = layout(
    [{ id: "a", x: 40, y: 40, pinned: true }],
    [{ source: "a", target: "hidden" }],
  );
  assert.deepEqual(a.nodes, [{ id: "a", x: 40, y: 40, pinned: true }]);
});
test("crown has finite extruded geometry for the crown and three memory layers", () => {
  const faces = crownMesh();
  assert.equal(faces.length, 27);
  assert.ok(
    faces.every(
      (f) =>
        f.points.length >= 4 &&
        f.points.every((p) => p.length === 3 && p.every(Number.isFinite)),
    ),
  );
  const depths = new Set(faces.flatMap((f) => f.points.map((p) => p[2])));
  assert.ok([...depths].some((v) => v < 0) && [...depths].some((v) => v > 0));
});

test("motion gate cancels hidden/offscreen/reduced motion, stops at deadline and idles without frames", () => {
  const names = [
    "document",
    "matchMedia",
    "requestAnimationFrame",
    "cancelAnimationFrame",
    "IntersectionObserver",
    "devicePixelRatio",
  ];
  const old = Object.fromEntries(
    names.map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)]),
  );
  const handlers = {},
    queue = new Map();
  let sequence = 0,
    intersection,
    mediaChange,
    toggle;
  const img = { hidden: false },
    canvas = {
      hidden: true,
      setAttribute() {},
      getContext: () => new Proxy({}, { get: () => () => {} }),
    };
  const button = {
    dataset: {},
    querySelector: () => img,
    prepend() {},
    after(v) {
      toggle = v;
    },
  };
  const media = {
    matches: false,
    addEventListener(_k, f) {
      mediaChange = f;
    },
  };
  try {
    Object.assign(globalThis, {
      document: {
        hidden: false,
        createElement: (t) => (t === "canvas" ? canvas : { setAttribute() {} }),
        addEventListener(k, f) {
          handlers[k] = f;
        },
      },
      matchMedia: () => media,
      devicePixelRatio: 3,
      requestAnimationFrame: (f) => {
        queue.set(++sequence, f);
        return sequence;
      },
      cancelAnimationFrame: (i) => queue.delete(i),
      IntersectionObserver: class {
        constructor(f) {
          intersection = f;
        }
        observe() {}
      },
    });
    installCrown(button);
    assert.equal(queue.size, 0);
    assert.equal(img.hidden, false);
    button.onclick();
    assert.equal(queue.size, 1);
    assert.equal(button.dataset.animating, "true");
    let [id, frame] = [...queue][0];
    queue.delete(id);
    frame(performance.now() + 100);
    assert.equal(queue.size, 1);
    assert.equal(canvas.width, 320);
    document.hidden = true;
    handlers.visibilitychange();
    assert.equal(queue.size, 0);
    assert.equal(img.hidden, false);
    document.hidden = false;
    media.matches = true;
    button.onclick();
    assert.equal(queue.size, 0);
    media.matches = false;
    button.onclick();
    media.matches = true;
    mediaChange();
    assert.equal(queue.size, 0);
    media.matches = false;
    button.onclick();
    intersection([{ isIntersecting: false }]);
    assert.equal(queue.size, 0);
    intersection([{ isIntersecting: true }]);
    button.onclick();
    [id, frame] = [...queue][0];
    queue.delete(id);
    frame(performance.now() + 2500);
    assert.equal(queue.size, 0);
    assert.equal(button.dataset.animating, "false");
    assert.equal(canvas.hidden, true);
    toggle.onclick();
    button.onclick();
    assert.equal(queue.size, 0);
    assert.equal(toggle.textContent, "Motion off");
  } finally {
    for (const name of names) {
      if (old[name]) Object.defineProperty(globalThis, name, old[name]);
      else delete globalThis[name];
    }
  }
});
