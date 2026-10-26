// Only the provider page is intercepted; app requests hit actual canonical stores.
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
  const callbacks = [];
  const pending = [];
  const attacker = `https://evil.example.invalid:${new URL(options.origin).port}`;
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === options.origin) return route.continue();
    if (
      url.origin === "https://github.com" &&
      url.pathname === "/login/oauth/authorize"
    ) {
      const query = Object.fromEntries(url.searchParams);
      assert.equal(query.scope, "");
      assert.equal(query.code_challenge_method, "S256");
      assert.equal(
        query.redirect_uri,
        `${options.origin}/viewer/auth/github/callback`,
      );
      await writeFile(options.exchange, JSON.stringify(query));
      const callback = `${query.redirect_uri}?state=${query.state}&code=controlled-code`;
      return route.fulfill({
        contentType: "text/html",
        body: `<html><title>Controlled provider</title><a id="continue" href="${callback.replaceAll("&", "&amp;")}">Continue</a></html>`,
      });
    }
    if (url.origin === attacker)
      return route.fulfill({
        contentType: "text/html",
        body: "<html><title>Hostile origin fixture</title></html>",
      });
    return route.abort();
  });
  const page = await context.newPage();
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/viewer/auth/github/callback")
      pending.push(
        request.allHeaders().then((headers) =>
          callbacks.push({
            origin_present: Object.hasOwn(headers, "origin"),
            method: request.method(),
            fetch_site: headers["sec-fetch-site"],
            fetch_mode: headers["sec-fetch-mode"],
          }),
        ),
      );
  });
  async function login() {
    await page.goto(`${options.origin}/viewer/`);
    const result = await page.evaluate(async () => {
      const context = await (await fetch("/viewer/auth/context")).json();
      const response = await fetch("/viewer/auth/github/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: context.csrf }),
      });
      return { status: response.status, body: await response.json() };
    });
    assert.equal(result.status, 200);
    await page.goto(result.body.authorization_url);
    await Promise.all([
      page.waitForURL(`${options.origin}/viewer/`),
      page.locator("#continue").click(),
    ]);
    assert.equal(new URL(page.url()).search, "");
    return page.evaluate(async () => {
      const response = await fetch("/viewer/api/v1/session");
      return { status: response.status, body: await response.json() };
    });
  }
  const first = await login();
  assert.equal(first.status, 200);
  assert.deepEqual(first.body.data.projects, ["p"]);
  const firstCookie = (await context.cookies()).find(
    (c) => c.name === "__Host-memoryking-viewer",
  );
  assert.ok(
    firstCookie.secure &&
      firstCookie.httpOnly &&
      firstCookie.sameSite === "Lax" &&
      firstCookie.path === "/",
  );
  assert.equal(await page.evaluate(() => document.cookie), "");
  assert.equal(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
    0,
  );
  const second = await login();
  assert.equal(second.status, 200);
  const secondCookie = (await context.cookies()).find(
    (c) => c.name === "__Host-memoryking-viewer",
  );
  assert.notEqual(firstCookie.value, secondCookie.value);
  const hostile = await context.newPage();
  await hostile.goto(attacker);
  const blocked = await hostile.evaluate(async (origin) => {
    try {
      await fetch(`${origin}/viewer/auth/logout-all`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: "x".repeat(64) }),
      });
      return false;
    } catch {
      return true;
    }
  }, options.origin);
  assert.ok(blocked);
  assert.equal(
    await page.evaluate(
      async () => (await fetch("/viewer/api/v1/session")).status,
    ),
    200,
  );
  assert.equal(
    await page.evaluate(
      async () => (await fetch("/viewer/auth/logout")).status,
    ),
    400,
  );
  const logout = await page.evaluate(
    async (csrf) =>
      (
        await fetch("/viewer/auth/logout-all", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ csrf }),
        })
      ).status,
    second.body.data.csrf,
  );
  assert.equal(logout, 200);
  assert.equal(
    await page.evaluate(
      async () => (await fetch("/viewer/api/v1/session")).status,
    ),
    401,
  );
  assert.ok(
    !(await context.cookies()).some(
      (c) => c.name === "__Host-memoryking-viewer",
    ),
  );
  await Promise.all(pending);
  assert.equal(callbacks.length, 2);
  assert.ok(
    callbacks.every((c) => !c.origin_present && c.method === "GET"),
    JSON.stringify(callbacks),
  );
  await writeFile(
    process.argv[3],
    JSON.stringify({
      passed: true,
      browser: await browser.version(),
      transport: options.transport,
      authentication:
        "controlled GitHub page and exchange; actual state/PKCE/canonical sessions",
      certificate: "fixture-only SPKI pin",
      callbacks,
      cookie_flags_verified: true,
      script_cannot_read_cookie: true,
      no_browser_persistent_auth_storage: true,
      session_rotated: true,
      same_site_subdomain_attack_blocked: true,
      durable_logout_all_verified: true,
    }),
  );
} finally {
  await browser.close();
}
