import assert from "node:assert/strict";
import test from "node:test";
import { layout } from "../../packages/dashboard-foundation/dist/graph-layout.js";
import { installEmblem } from "../../packages/dashboard-foundation/dist/brand.js";
import {
  AUTHORITY_LEGEND,
  authorityOf,
  nodeRadius,
} from "../../packages/dashboard-foundation/dist/graph.js";

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
test("emblem motion is on request only, bounded and cancelled when hidden, offscreen or reduced", () => {
  const names = ["document", "matchMedia", "IntersectionObserver"];
  const old = Object.fromEntries(
    names.map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)]),
  );
  const handlers = {};
  let intersection,
    mediaChange,
    active = null,
    starts = 0;
  const img = {
    animate(_frames, options) {
      assert.equal(options.iterations, 1);
      assert.ok(options.duration <= 2400);
      starts++;
      active = {
        cancel() {
          active = null;
        },
        onfinish: null,
      };
      return active;
    },
  };
  const button = {
    dataset: {},
    querySelector: () => img,
    after() {
      assert.fail("The emblem must not add a separate motion control");
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
        createElement: () => ({ setAttribute() {} }),
        addEventListener(k, f) {
          handlers[k] = f;
        },
      },
      matchMedia: () => media,
      IntersectionObserver: class {
        constructor(f) {
          intersection = f;
        }
        observe() {}
      },
    });
    installEmblem(button);
    assert.equal(starts, 0);
    button.onclick();
    assert.equal(button.dataset.animating, "true");
    assert.ok(active);
    document.hidden = true;
    handlers.visibilitychange();
    assert.equal(active, null);
    button.onclick();
    assert.equal(starts, 1);
    document.hidden = false;
    media.matches = true;
    button.onclick();
    assert.equal(active, null);
    media.matches = false;
    button.onclick();
    media.matches = true;
    mediaChange();
    assert.equal(active, null);
    media.matches = false;
    button.onclick();
    intersection([{ isIntersecting: false }]);
    assert.equal(active, null);
    button.onclick();
    assert.equal(active, null);
    intersection([{ isIntersecting: true }]);
    button.onclick();
    active.onfinish();
    assert.equal(active, null);
    assert.equal(button.dataset.animating, "false");
    delete img.animate;
    button.onclick();
    assert.equal(button.dataset.animating, "false");
  } finally {
    for (const name of names) {
      if (old[name]) Object.defineProperty(globalThis, name, old[name]);
      else delete globalThis[name];
    }
  }
});

test("node colour follows recorded authority and size follows citations, bounded", () => {
  assert.equal(authorityOf({ type: "observation" }), "observation");
  assert.equal(
    authorityOf({
      type: "claim",
      state: "accepted",
      authority: "accepted_knowledge",
    }),
    "accepted",
  );
  assert.equal(
    authorityOf({
      type: "claim",
      state: "proposed",
      authority: "proposed_interpretation",
    }),
    "proposed",
  );
  assert.equal(
    authorityOf({ type: "claim", state: "accepted", quarantined: true }),
    "quarantined",
  );
  assert.equal(authorityOf({ type: "claim", state: "rejected" }), "retired");
  assert.equal(
    authorityOf({
      type: "claim",
      state: "accepted",
      authority: "mandatory_constraint",
    }),
    "constraint",
  );
  assert.equal(authorityOf({ type: "attestation" }), "attestation");
  assert.equal(authorityOf({ type: "procedure" }), "procedure");
  for (const type of ["checkpoint", "task", "run", "unknown"])
    assert.equal(authorityOf({ type }), "working");
  for (const type of ["plan", "step"])
    assert.equal(authorityOf({ type }), "plan");
  const legend = new Set(AUTHORITY_LEGEND.map(([k]) => k));
  for (const type of [
    "observation",
    "claim",
    "attestation",
    "procedure",
    "plan",
    "step",
    "task",
  ])
    assert.ok(legend.has(authorityOf({ type, state: "proposed" })));
  assert.equal(nodeRadius(0), 8);
  assert.equal(nodeRadius(undefined), 8);
  assert.equal(nodeRadius(-5), 8);
  assert.equal(nodeRadius(4), 12);
  assert.equal(nodeRadius(10000), 16);
  assert.equal(nodeRadius(4, true), 15);
  assert.ok(Number.isFinite(nodeRadius(Number.NaN)));
});
