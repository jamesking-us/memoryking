// Real desktop Chromium, isolated temporary profile and one fixture certificate pin.
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const options = JSON.parse(await readFile(process.argv[2], "utf8"));
const { chromium } = await import(pathToFileURL(options.playwright).href);
const browser = await chromium.launch({
  executablePath: options.chrome,
  headless: true,
  args: [
    "--no-proxy-server",
    "--host-resolver-rules=MAP viewer.example.invalid 127.0.0.1",
    `--ignore-certificate-errors-spki-list=${options.spki}`,
  ],
});
try {
  const context = await browser.newContext();
  await context.route("**/*", (route) =>
    new URL(route.request().url()).origin === options.origin
      ? route.continue()
      : route.abort(),
  );
  await context.addCookies([
    {
      name: "__Host-memoryking-viewer",
      value: "a".repeat(48),
      url: options.origin,
      secure: true,
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
  const page = await context.newPage();
  const requests = [];
  const pending = [];
  page.on("request", (request) => {
    pending.push(
      request.allHeaders().then((headers) => {
        requests.push({
          path: new URL(request.url()).pathname,
          method: request.method(),
          origin_present: Object.hasOwn(headers, "origin"),
        });
      }),
    );
  });
  const navigation = await page.goto(`${options.origin}/viewer/`);
  assert.equal(navigation.status(), 200);
  assert.equal(await page.title(), "MemoryKing · Project dashboard");
  const reads = await page.evaluate(async () => {
    const results = [];
    for (const path of [
      "/viewer/shell.css",
      "/viewer/api/v1/session",
      "/viewer/api/v1/projects/p/memories",
    ]) {
      for (const method of ["GET", "HEAD"]) {
        const response = await fetch(path, { method });
        const text = await response.text();
        results.push({
          path,
          method,
          status: response.status,
          cache_control: response.headers.get("cache-control"),
          empty: text.length === 0,
          kind:
            method === "GET" && path.includes("/api/")
              ? JSON.parse(text).data.kind
              : null,
        });
      }
    }
    return results;
  });
  await Promise.all(pending);
  for (const result of reads) {
    assert.equal(result.status, 200, JSON.stringify(result));
    assert.equal(result.cache_control, "no-store");
    assert.equal(result.empty, result.method === "HEAD");
  }
  assert.equal(reads.find((r) => r.kind === "session").status, 200);
  assert.equal(reads.find((r) => r.kind === "memories").status, 200);
  assert.ok(requests.length >= 8);
  assert.ok(requests.every((request) => !request.origin_present));
  await writeFile(
    process.argv[3],
    JSON.stringify({
      passed: true,
      browser: await browser.version(),
      transport: options.transport,
      certificate: "isolated fixture SPKI pin; no global TLS ignore",
      authentication:
        "internal synthetic resolver; not OAuth or production login",
      navigation_status: navigation.status(),
      requests,
      reads,
    }),
  );
} finally {
  await browser.close();
}
