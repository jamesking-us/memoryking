import test from "node:test";
import assert from "node:assert/strict";
import { ViewerClient } from "../../packages/mobile-viewer/dist/client.js";
import { HttpError } from "../../packages/mobile-viewer/dist/network.js";

const binding = (project = "p", changes = {}) => ({
  store: "store_a",
  tenant: "main",
  principal: "phone",
  project,
  incarnation: "history",
  authorization_epoch: 1,
  session_generation: 1,
  evaluated_ms: 100000,
  fresh_until_ms: 115000,
  observed_position: 1,
  ...changes,
});
const response = (kind, data = {}, changes = {}) => ({
  schema_version: 1,
  binding: binding(kind === "session" ? "_session" : "p", changes),
  coverage: "bounded",
  truncated: false,
  next_cursor: null,
  data: { kind, ...(kind === "session" ? { poll_seconds: 10 } : {}), ...data },
});
const session = (changes = {}, csrf = "a".repeat(64)) =>
  response("session", { projects: ["p", "q"], csrf }, changes);
function setup() {
  const time = { mono: 1000, utc: 100000, visible: true, online: true };
  const calls = [],
    timers = new Map(),
    views = [];
  let timer = 0;
  const fixture = {
    time,
    calls,
    timers,
    views,
    next: async (path) =>
      path.endsWith("/session")
        ? session()
        : response("overview", { tasks: [] }),
  };
  fixture.client = new ViewerClient(
    (state) =>
      views.push({
        status: state.status,
        data: state.response?.data.kind,
        project: state.project,
      }),
    {
      clock: { monotonic: () => time.mono, wall: () => time.utc },
      visible: () => time.visible,
      online: () => time.online,
      request: async (...args) => {
        calls.push(args[0]);
        return fixture.next(...args);
      },
      random: () => 1,
      timer: (cb, ms) => {
        const id = ++timer;
        timers.set(id, { cb, ms });
        return id;
      },
      cancel: (id) => timers.delete(id),
    },
  );
  return fixture;
}
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
const tick = async () => {
  await new Promise(setImmediate);
};

test("one active view follows a current session check, polling is visible-only and bounded", async () => {
  const f = setup();
  await f.client.refresh();
  assert.deepEqual(f.calls, [
    "/viewer/api/v1/session",
    "/viewer/api/v1/projects/p/overview",
  ]);
  assert.equal(f.client.state.status, "ready");
  assert.equal(f.timers.size, 2);
  assert.deepEqual(
    [...f.timers.values()].map((x) => x.ms).sort((a, b) => a - b),
    [10000, 15000],
  );
  f.time.visible = false;
  f.client.pause();
  await f.client.refresh();
  assert.equal(f.calls.length, 2);
  assert.equal(f.timers.size, 0);
  assert.equal(f.client.state.session, null);
  assert.equal(f.client.state.project, "");
});
test("abort failure cannot repaint a reply held across background, offline or a project change", async () => {
  for (const transition of ["pause", "offline", "resume"]) {
    const f = setup();
    await f.client.refresh();
    const held = deferred();
    f.next = (path) => (path.endsWith("/session") ? session() : held.promise);
    const old = f.client.refresh();
    await tick();
    f.client[transition]();
    if (transition === "resume") f.client.pause();
    held.resolve(
      response("overview", { tasks: [{ objective: "PRIVATE_OLD_MARKER" }] }),
    );
    await old;
    await tick();
    assert.equal(f.client.state.response, null);
    assert.equal(f.client.state.session, null);
  }
  const f = setup();
  await f.client.refresh();
  const held = deferred();
  let reads = 0;
  f.next = (path) =>
    path.endsWith("/session")
      ? session()
      : ++reads === 1
        ? held.promise
        : response("overview", { tasks: [] }, { project: "q" });
  const old = f.client.refresh();
  await tick();
  f.client.select({ tab: "overview", kind: "overview" }, "q");
  await tick();
  assert.equal(f.client.state.project, "q");
  held.resolve(
    response("overview", { tasks: [{ objective: "PRIVATE_OLD_MARKER" }] }),
  );
  await old;
  assert.equal(f.client.state.project, "q");
  assert.deepEqual(f.client.state.response.data.tasks, []);
});
test("newer refresh wins when an old request ignores AbortSignal", async () => {
  const f = setup();
  const first = deferred();
  let count = 0;
  f.next = (path) =>
    path.endsWith("/session")
      ? ++count === 1
        ? first.promise
        : session()
      : response("overview", { tasks: [] });
  const old = f.client.refresh();
  await f.client.refresh();
  first.resolve(session({ principal: "OLD_ACCOUNT" }));
  await old;
  assert.equal(f.client.state.session.binding.principal, "phone");
  assert.equal(f.client.state.status, "ready");
});
test("every authority dimension and opaque-session CSRF rotation clears before reuse", async () => {
  for (const [key, value] of Object.entries({
    store: "other",
    tenant: "other",
    principal: "other",
    incarnation: "other",
    authorization_epoch: 2,
    session_generation: 2,
  })) {
    const f = setup();
    await f.client.refresh();
    f.next = () => session({ [key]: value });
    await f.client.refresh();
    assert.equal(f.client.state.response, null, key);
    assert.equal(f.client.state.project, "", key);
  }
  const f = setup();
  await f.client.refresh();
  f.next = () => session({}, "b".repeat(64));
  await f.client.refresh();
  assert.equal(f.client.state.session, null);
});
test("session and data bindings must agree, and an observed frontier cannot move backward", async () => {
  const f = setup();
  f.next = (path) =>
    path.endsWith("/session")
      ? session({ observed_position: 2 })
      : response("overview", { tasks: [] });
  await f.client.refresh();
  assert.equal(f.client.state.response, null);
  f.next = (path) =>
    path.endsWith("/session")
      ? session()
      : response("overview", { tasks: [] }, { authorization_epoch: 2 });
  f.time.mono += 1001;
  f.time.utc += 1001;
  await f.client.refresh();
  assert.equal(f.client.state.response, null);
});
test("logout clears synchronously and late data cannot revive it; ambiguous logout is honest", async () => {
  const f = setup();
  await f.client.refresh();
  const held = deferred();
  f.next = (path) =>
    path.endsWith("/session")
      ? session()
      : path.endsWith("/logout")
        ? Promise.reject(new Error("network"))
        : held.promise;
  const read = f.client.refresh();
  await tick();
  const logout = f.client.logout();
  assert.equal(f.client.state.response, null);
  await logout;
  assert.match(f.client.state.message, /could not be confirmed/);
  held.resolve(response("overview", { tasks: [] }));
  await read;
  assert.equal(f.client.state.status, "signed_out");
  assert.equal(f.timers.size, 0);
});
test("request failure drops private state, honors long Retry-After and manual/lifecycle retries cannot bypass it", async () => {
  const f = setup();
  await f.client.refresh();
  f.next = () => Promise.reject(new HttpError(429, "120", "rate_limited"));
  await f.client.refresh();
  assert.equal(f.client.state.response, null);
  const count = f.calls.length;
  await f.client.refresh();
  f.client.resume();
  await tick();
  assert.equal(f.calls.length, count);
  assert.equal([...f.timers.values()][0].ms, 120000);
  f.time.mono += 120000;
  f.time.utc += 120000;
  f.next = (path) =>
    path.endsWith("/session") ? session() : response("overview", { tasks: [] });
  await f.client.refresh();
  assert.equal(f.client.state.status, "ready");
});
test("expiry is enforced synchronously without timers and a late report fragment cannot survive clearing", async () => {
  const f = setup();
  await f.client.refresh();
  f.time.mono += 15001;
  f.time.utc += 15001;
  assert.equal(f.client.check(), false);
  assert.equal(f.client.state.session, null);
  const g = setup();
  await g.client.refresh();
  const held = deferred();
  g.next = (path) =>
    path.endsWith("/session")
      ? session()
      : path.includes("/sections/")
        ? held.promise
        : response("report", { report: "r", revision: 1, sections: 1 });
  g.client.select({ tab: "overview", kind: "report", id: "r", revision: 1 });
  await tick();
  g.client.pause();
  held.resolve(
    response("report_section", {
      page: 0,
      pages: 1,
      content: {
        version: 1,
        block: 0,
        part: 0,
        parts: 1,
        encoding: "canonical-block-json",
        content: JSON.stringify({
          kind: "paragraph",
          text: "PRIVATE_REPORT_MARKER",
        }),
      },
    }),
  );
  await tick();
  assert.equal(g.client.state.report, null);
  assert.equal(g.client.state.response, null);
});
test("login cannot navigate to arbitrary origins and a cancelled context cannot start exchange", async () => {
  for (const authorization_url of [
    "https://evil.invalid/login/oauth/authorize",
    "https://github.com:123/login/oauth/authorize",
    "https://github.com/login/oauth/authorize#fragment",
    "https://user:pass@github.com/login/oauth/authorize",
  ]) {
    const f = setup();
    f.next = (path) =>
      path.endsWith("/context")
        ? { schema_version: 1, csrf: "a".repeat(64) }
        : { schema_version: 1, authorization_url };
    assert.equal(await f.client.login(), null);
  }
  const f = setup(),
    held = deferred();
  f.next = () => held.promise;
  const login = f.client.login();
  f.client.pause();
  held.resolve({ schema_version: 1, csrf: "a".repeat(64) });
  assert.equal(await login, null);
  assert.equal(f.calls.length, 1);
});
test("login context accepts the server's opaque base64url CSRF rather than assuming session HMAC format", async () => {
  const f = setup(),
    csrf = "X_x-9".repeat(8),
    url = "https://github.com/login/oauth/authorize?client_id=test";
  f.next = (path, signal, body) => {
    if (path.endsWith("/context")) return { schema_version: 1, csrf };
    assert.equal(body.csrf, csrf);
    return { schema_version: 1, authorization_url: url };
  };
  assert.equal(await f.client.login(), url);
});
test("configured polling honors 5-60 seconds without extending the private display lease", async () => {
  for (const seconds of [5, 60]) {
    const f = setup();
    f.next = (path) =>
      path.endsWith("/session")
        ? response("session", {
            projects: ["p"],
            csrf: "a".repeat(64),
            poll_seconds: seconds,
          })
        : response("overview", { tasks: [] });
    await f.client.refresh();
    assert.ok(
      [...f.timers.values()].some((timer) => timer.ms === seconds * 1000),
    );
    if (seconds === 60) {
      f.time.mono += 15001;
      f.time.utc += 15001;
      assert.equal(f.client.check(), false);
      assert.equal(f.client.state.response, null);
      assert.deepEqual(
        [...f.timers.values()].map((timer) => timer.ms),
        [44999],
      );
    }
  }
});
test("a successful memory detail refresh uses its new short-lived handle next time", async () => {
  const f = setup();
  await f.client.refresh();
  let count = 0;
  f.next = (path) =>
    path.endsWith("/session")
      ? session()
      : response("memory", { memories: [{ handle: `renewed_${++count}` }] });
  f.client.select({ tab: "memories", kind: "memory", id: "initial" });
  await tick();
  assert.equal(f.client.state.selection.id, "renewed_1");
  await f.client.refresh();
  assert.equal(f.calls.at(-1), "/viewer/api/v1/projects/p/memories/renewed_1");
  assert.equal(f.client.state.selection.id, "renewed_2");
});
test("late figure chunks after clearing cannot create or retain an object URL", async () => {
  const f = setup();
  await f.client.refresh();
  f.next = (path) =>
    path.endsWith("/session")
      ? session()
      : path.includes("/sections/")
        ? response("report_section", {
            page: 0,
            pages: 1,
            content: {
              version: 1,
              block: 0,
              part: 0,
              parts: 1,
              encoding: "canonical-block-json",
              content: JSON.stringify({
                kind: "figure",
                caption: "Synthetic",
                reference: "image.png",
              }),
            },
          })
        : response("report", { report: "r", revision: 1, sections: 1 });
  f.client.select({ tab: "overview", kind: "report", id: "r", revision: 1 });
  await tick();
  assert.equal(f.client.state.status, "ready");
  const held = deferred();
  f.next = () => held.promise;
  const image = f.client.figure("image.png");
  f.client.pause();
  held.resolve(
    response("report_image", {
      name: "image.png",
      bytes: 1,
      offset: 0,
      next_offset: null,
      sha256: "a".repeat(64),
      data_base64: "AA==",
    }),
  );
  await image;
  assert.equal(f.client.state.figures.size, 0);
  assert.equal(f.client.state.report, null);
  assert.equal(f.client.state.response, null);
});
