import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import {
  MemoryKing,
  MemoryKingError,
  MemoryKingLocal,
} from "../../packages/client/dist/index.js";

const python =
  process.env.MEMORYKING_PYTHON ??
  (existsSync(".venv/bin/python") ? ".venv/bin/python" : "python3");

/** Start a real service through tools/client_test_service.py; stop it by closing stdin. */
async function service(backend) {
  const child = spawn(python, ["tools/client_test_service.py", backend], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  const description = await new Promise((resolve, reject) => {
    let buffer = "";
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.includes("\n")) resolve(JSON.parse(buffer.split("\n")[0]));
    });
    child.on("exit", (code) =>
      reject(new Error(`${backend} service exited early: ${code}`)),
    );
  });
  const stopped = new Promise((resolve) => child.on("exit", resolve));
  return {
    ...description,
    async stop() {
      child.stdin.end();
      await stopped;
    },
  };
}

async function refused(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof MemoryKingError, String(error));
    assert.equal(error.code, code, error.message);
    return true;
  });
}

test("client refuses unsafe URLs and ambiguous credentials before any request", () => {
  const base = { tenant: "t", project: "p", credential: "x" };
  assert.throws(() => new MemoryKing({ ...base, url: "http://example.test" }), {
    code: "shared_tls_required",
  });
  assert.throws(() => new MemoryKing({ ...base, url: "http://127.0.0.1:1" }), {
    code: "shared_tls_required",
  });
  assert.throws(
    () => new MemoryKing({ ...base, url: "https://u:p@example.test" }),
    {
      code: "invalid_shared_url",
    },
  );
  assert.throws(
    () => new MemoryKing({ ...base, url: "https://example.test/x?y=1" }),
    {
      code: "invalid_shared_url",
    },
  );
  assert.throws(
    () =>
      new MemoryKing({ ...base, url: "https://example.test", project: "a/b" }),
    {
      code: "invalid_request",
    },
  );
  assert.throws(
    () =>
      new MemoryKing({
        url: "https://example.test",
        tenant: "t",
        project: "p",
      }),
    {
      code: "credential_required",
    },
  );
  assert.throws(() => MemoryKing.connect({}, {}), {
    code: "shared_url_required",
  });
  assert.throws(() => MemoryKing.connect({}, { MEMORYKING_URL: "https://h" }), {
    code: "tenant_required",
  });
  const connected = MemoryKing.connect(
    {},
    {
      MEMORYKING_URL: "https://h.example",
      MEMORYKING_TENANT: "t",
      MEMORYKING_PROJECT: "p",
      MEMORYKING_CREDENTIAL: "/nonexistent/file",
    },
  );
  assert.equal(connected.project, "p");
});

test("local daemon: recall over the loopback agent endpoint", async () => {
  const daemon = await service("local");
  try {
    const memory = new MemoryKingLocal({
      port: daemon.port,
      project: daemon.project,
      incarnation: daemon.incarnation,
      credentialFile: daemon.credential_file,
    });
    const briefing = await memory.recall("timeout deadline");
    assert.match(
      briefing.text,
      /Raising every timeout exceeded the total retry deadline/,
    );
    assert.equal(briefing.sources[0].ref, "g1");
    assert.ok(briefing.receipt.startsWith("rcpt_"));
    const structured = await memory.recall("timeout deadline", {
      format: "json",
      asOf: new Date(Date.now() + 86_400_000),
    });
    assert.equal(structured.text, "");
    assert.ok(Array.isArray(structured.raw.guidance));
    const early = await memory.recall("timeout deadline", {
      asOf: "2001-01-01",
    });
    assert.doesNotMatch(early.text, /Raising every timeout/);

    const stale = new MemoryKingLocal({
      port: daemon.port,
      project: daemon.project,
      incarnation: "hist_" + "0".repeat(32),
      credentialFile: daemon.credential_file,
    });
    await assert.rejects(stale.recall("timeout"), (error) => {
      assert.ok(error instanceof MemoryKingError);
      assert.equal(error.code, "history_changed");
      assert.equal(error.status, 409);
      assert.ok(error.hint);
      return true;
    });
    const wrong = new MemoryKingLocal({
      port: daemon.port,
      project: daemon.project,
      incarnation: daemon.incarnation,
      credential: "not-a-credential",
    });
    await assert.rejects(wrong.recall("timeout"), MemoryKingError);
    const missing = new MemoryKingLocal({
      port: daemon.port,
      project: daemon.project,
      incarnation: daemon.incarnation,
      credentialFile: "/nonexistent/credential",
    });
    await refused(missing.recall("timeout"), "credential_required");
    assert.equal(
      typeof memory.remember,
      "undefined",
      "the endpoint serves retrieval only",
    );
  } finally {
    await daemon.stop();
  }
});

const postgres = process.env.MEMORYKING_PG_BIN;
const havePostgres = Boolean(postgres) && existsSync(`${postgres}/pg_ctl`);
const sharedTest =
  havePostgres || process.env.MEMORYKING_REQUIRE_POSTGRES === "1"
    ? test
    : test.skip;

sharedTest("shared service: the five operations over MCP HTTP", async () => {
  const shared = await service("shared");
  try {
    const options = {
      url: shared.url,
      tenant: shared.tenant,
      project: shared.project,
      loopbackHttp: true,
    };
    const memory = new MemoryKing({
      ...options,
      credentialFile: shared.credential_file,
    });

    const saved = await memory.remember(
      "Increasing every timeout exceeded the deadline",
      {
        source: "pytest",
      },
    );
    assert.ok(saved.observation.startsWith("obs_"));
    assert.equal(saved.held, false);
    const again = await memory.remember(
      "Increasing every timeout exceeded the deadline",
      {
        source: "pytest",
      },
    );
    assert.deepEqual(
      again.raw,
      saved.raw,
      "the same text on the same day replays",
    );

    const briefing = await memory.recall("timeout deadline");
    assert.match(
      briefing.text,
      /Increasing every timeout exceeded the deadline/,
    );
    assert.equal(briefing.sources[0].id, saved.observation);
    const structured = await memory.recall("timeout deadline", {
      format: "json",
    });
    assert.equal(structured.text, "");

    const first = await memory.saveProgress(
      "retry-fix",
      "Fix the retry deadline",
      "Investigating",
      {
        nextActions: ["Run the regression"],
        failedAttempts: [
          { action: "Raise every timeout", outcome: "Still failed" },
        ],
        blockers: ["Need a full regression result"],
        shareWith: ["reviewer"],
      },
    );
    assert.equal(first.revision, 1);
    const second = await memory.saveProgress(
      "retry-fix",
      "Fix the retry deadline",
      "Fix drafted",
    );
    assert.equal(second.revision, 2);
    const note = await memory.resume("retry-fix");
    assert.match(note.text, /Fix drafted/);
    assert.match(
      note.text,
      /Run the regression/,
      "an omitted section carries over",
    );
    assert.match(note.text, /Need a full regression result/);

    const handed = await memory.handOff("retry-fix", "reviewer");
    assert.equal(handed.status, "prepared");
    assert.ok(handed.handle);

    const context = await memory.context();
    assert.equal(context.principal, "owner");

    await refused(
      memory.handOff("never-saved", "reviewer"),
      "checkpoint_unavailable",
    );
    await assert.rejects(memory.recall(""), (error) => {
      assert.ok(error instanceof MemoryKingError);
      assert.equal(error.code, "invalid_tool_arguments");
      return true;
    });

    const reader = new MemoryKing({
      ...options,
      credentialFile: shared.credentials.reader,
    });
    await assert.rejects(reader.remember("not allowed"), (error) => {
      assert.ok(error instanceof MemoryKingError);
      assert.equal(error.code, "denied");
      assert.ok(error.hint && error.hint.length <= 200);
      return true;
    });
    const stranger = new MemoryKing({
      ...options,
      credential: "not-a-credential",
    });
    await assert.rejects(stranger.recall("timeout"), MemoryKingError);
    const elsewhere = new MemoryKing({
      ...options,
      project: "other",
      credentialFile: shared.credential_file,
    });
    const other = await elsewhere.recall("timeout deadline");
    assert.doesNotMatch(
      other.text,
      /Increasing every timeout/,
      "projects stay apart",
    );

    const unreachable = new MemoryKing({
      ...options,
      url: "http://127.0.0.1:1",
      credential: "x",
      timeoutMs: 2000,
    });
    await refused(unreachable.recall("x"), "transport_unavailable");
  } finally {
    await shared.stop();
  }
});
