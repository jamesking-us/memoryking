import test from "node:test";
import assert from "node:assert/strict";
import {
  Freshness,
  envelope,
  sameAuthority,
  retryDelay,
} from "../../packages/mobile-viewer/dist/freshness.js";
import {
  ReportBuffer,
  safeLink,
} from "../../packages/mobile-viewer/dist/report.js";

const binding = (changes = {}) => ({
  store: "store_a",
  tenant: "main",
  principal: "viewer",
  project: "project",
  incarnation: "hist_a",
  authorization_epoch: 1,
  session_generation: 1,
  evaluated_ms: 100000,
  fresh_until_ms: 115000,
  observed_position: 7,
  ...changes,
});
function clock() {
  const time = { mono: 1000, utc: 100000, clears: 0 };
  const gate = new Freshness(
    { monotonic: () => time.mono, wall: () => time.utc },
    () => time.clears++,
  );
  return { time, gate };
}

test("private view subtracts request time and expires synchronously even without a timer", () => {
  const { time, gate } = clock();
  const ticket = gate.ticket(gate.startCycle());
  time.mono += 4000;
  time.utc += 4000;
  gate.commit(ticket, [gate.receive(ticket, binding())]);
  assert.equal(gate.remaining(), 11000);
  time.mono += 11000;
  time.utc += 11000;
  assert.equal(gate.check(), false);
  assert.equal(time.clears, 1);
});
test("server release deadline and earliest composed lease cannot be extended by client", () => {
  const { time, gate } = clock();
  const ticket = gate.ticket(gate.startCycle());
  time.mono += 2000;
  time.utc += 2000;
  const far = gate.receive(ticket, binding({ fresh_until_ms: 999999 }));
  const early = gate.receive(ticket, binding({ fresh_until_ms: 103000 }));
  gate.commit(ticket, [far, early]);
  assert.equal(gate.remaining(), 1000);
  time.mono += 1001;
  time.utc += 1001;
  assert.equal(gate.check(), false);
});
test("late replies after logout, project/background clearing and a newer cycle cannot commit", () => {
  const { gate } = clock();
  const old = gate.ticket(gate.startCycle());
  gate.clear();
  assert.throws(() => gate.receive(old, binding()), /superseded/);
  const later = gate.ticket(gate.startCycle());
  const lease = gate.receive(later, binding());
  gate.startCycle();
  assert.throws(() => gate.commit(later, [lease]), /superseded/);
});
test("wall rollback fails closed and sleeping monotonic clock cannot preserve a view", () => {
  const { time, gate } = clock();
  const ticket = gate.ticket(gate.startCycle());
  gate.commit(ticket, [gate.receive(ticket, binding())]);
  time.utc -= 100;
  assert.equal(gate.check(), false);
  assert.throws(() => gate.ticket(gate.startCycle()), /clock_changed/);
  time.utc += 101;
  const resumed = gate.ticket(gate.startCycle());
  gate.commit(resumed, [gate.receive(resumed, binding())]);
  time.utc += 16000;
  assert.equal(gate.check(), false);
});
test("a delayed request cannot establish a fresh display after its entire allowance elapsed", () => {
  const { time, gate } = clock();
  const ticket = gate.ticket(gate.startCycle());
  time.mono += 15001;
  time.utc += 15001;
  assert.throws(() => gate.receive(ticket, binding()), /expired/);
});
test("authorization comparison fences every identity dimension and response project", () => {
  for (const [field, value] of Object.entries({
    store: "other",
    tenant: "other",
    principal: "other",
    incarnation: "other",
    authorization_epoch: 2,
    session_generation: 2,
  }))
    assert.equal(
      sameAuthority(binding(), binding({ [field]: value })),
      false,
      field,
    );
  assert.equal(
    sameAuthority(
      binding(),
      binding({
        evaluated_ms: 110000,
        observed_position: 8,
        project: "_session",
      }),
    ),
    true,
  );
  const raw = {
    schema_version: 1,
    binding: binding(),
    coverage: "bounded",
    truncated: false,
    next_cursor: null,
    data: { kind: "tasks", tasks: [] },
  };
  assert.equal(envelope(raw, "tasks", "project"), raw);
  assert.throws(() => envelope(raw, "tasks", "other"), /invalid_response/);
  assert.throws(
    () =>
      envelope(
        { ...raw, binding: binding({ fresh_until_ms: 100000 }) },
        "tasks",
        "project",
      ),
    /invalid_response/,
  );
  assert.throws(
    () =>
      envelope(
        { ...raw, binding: binding({ session_generation: NaN }) },
        "tasks",
        "project",
      ),
    /invalid_response/,
  );
});
test("backoff caps local jitter while honoring a longer server retry interval", () => {
  assert.equal(
    retryDelay(10, null, () => 1, 0),
    60000,
  );
  assert.equal(
    retryDelay(1, "120", () => 0, 0),
    120000,
  );
  assert.equal(
    retryDelay(1, "Thu, 01 Jan 1970 00:02:00 GMT", () => 0, 0),
    120000,
  );
  assert.equal(
    retryDelay(1, "bad", () => 0, 0),
    16000,
  );
});
test("lossless report fragments reconstruct long Unicode code and tables, with no skipped block", () => {
  const code = {
    kind: "code",
    text: "const label = '<script>世界</script>';\n".repeat(150),
  };
  const raw = JSON.stringify(code);
  const parts = [];
  for (let n = 0; n < raw.length; n += 4096) parts.push(raw.slice(n, n + 4096));
  const buffer = new ReportBuffer(parts.length + 1);
  parts.forEach((content, i) =>
    buffer.append(
      i,
      parts.length + 1,
      {
        version: 1,
        block: 0,
        part: i,
        parts: parts.length,
        encoding: "canonical-block-json",
        content,
      },
      "next",
    ),
  );
  assert.deepEqual(buffer.blocks, [code]);
  assert.equal(buffer.complete, false);
  const table = {
    kind: "table",
    headers: ["State", "Evidence"],
    rows: [["unknown", "Not evaluated"]],
  };
  buffer.append(
    parts.length,
    parts.length + 1,
    {
      version: 1,
      block: 1,
      part: 0,
      parts: 1,
      encoding: "canonical-block-json",
      content: JSON.stringify(table),
    },
    null,
  );
  assert.equal(buffer.complete, true);
  assert.deepEqual(buffer.blocks, [code, table]);
  buffer.clear();
  assert.deepEqual(buffer.blocks, []);
  assert.equal(buffer.cursor, null);
});
test("report reader rejects out-of-order fragments, silently missing content and unsafe source links", () => {
  const fragment = {
    version: 1,
    block: 0,
    part: 0,
    parts: 1,
    encoding: "canonical-block-json",
    content: JSON.stringify({ kind: "paragraph", text: "inert" }),
  };
  assert.throws(
    () => new ReportBuffer(2).append(1, 2, fragment, null),
    /invalid_report/,
  );
  assert.throws(
    () => new ReportBuffer(2).append(0, 2, fragment, null),
    /invalid_report/,
  );
  assert.throws(
    () => new ReportBuffer(1).append(0, 1, { ...fragment, block: 1 }, null),
    /invalid_report/,
  );
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,x",
    "//example.com",
    "https://name:pass@example.com",
    "https://example.com/\ntrack",
  ])
    assert.equal(safeLink(url), null);
  assert.equal(
    safeLink("https://example.com/evidence"),
    "https://example.com/evidence",
  );
});
test("server fragment bounds count Unicode code points, not JavaScript UTF-16 units", () => {
  const block = { kind: "code", text: "🦣".repeat(4090) };
  const raw = [...JSON.stringify(block)],
    parts = [];
  for (let i = 0; i < raw.length; i += 4096)
    parts.push(raw.slice(i, i + 4096).join(""));
  assert.ok(parts[0].length > 4096);
  const buffer = new ReportBuffer(parts.length);
  parts.forEach((content, i) =>
    buffer.append(
      i,
      parts.length,
      {
        version: 1,
        block: 0,
        part: i,
        parts: parts.length,
        encoding: "canonical-block-json",
        content,
      },
      i + 1 === parts.length ? null : "next",
    ),
  );
  assert.deepEqual(buffer.blocks, [block]);
});
test("a numeric Retry-After beyond floating-point range must not become an immediate retry", () => {
  assert.equal(
    retryDelay(0, "9".repeat(400), () => 1, 0),
    Infinity,
  );
});
