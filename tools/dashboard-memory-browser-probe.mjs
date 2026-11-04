import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
const options = JSON.parse(await readFile(process.argv[2], "utf8"));
const { chromium } = await import(pathToFileURL(options.playwright).href);
const browser = await chromium.launch({
  executablePath: options.chrome,
  headless: true,
});
const checks = [];
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1050 },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(options.url);
  // Actual compiled component and DOM, injected clock/request faults. No real credentials.
  const result = await page.evaluate(async () => {
    const { Observatory } = await import("/observatory.js");
    const originalWall = Date.now,
      originalMono = performance.now.bind(performance);
    let wall = originalWall(),
      mono = originalMono(),
      authority = {
        csrf: "synthetic",
        tenant: "t",
        principal: "p",
        incarnation: "h",
        authorization_epoch: 1,
      };
    Date.now = () => wall;
    Object.defineProperty(performance, "now", {
      configurable: true,
      value: () => mono,
    });
    const root = document.createElement("section");
    document.body.append(root);
    let calls = [],
      held = null,
      reject = false,
      hold = false,
      rejectAction = "",
      number = 0;
    const api = async (_path, body) => {
      calls.push({
        path: _path,
        operation: body.operation,
        action: body.command.action,
      });
      const value = {
        incarnation: "h",
        tenant: "t",
        principal: "p",
        project: "demo",
        evaluated_ms: wall,
        fresh_until_ms: wall + 15000,
        receipt: "r" + ++number,
        frames: [],
        projection: { tasks: [] },
        marker: "PRIVATE_FIXTURE",
      };
      if (reject || body.command.action === rejectAction)
        throw Error("session_changed");
      if (hold)
        return await new Promise((resolve) => {
          held = () => resolve(value);
        });
      return value;
    };
    const view = new Observatory(root, api, () => authority);
    const flush = async () => {
      for (let n = 0; n < 12; n++) await Promise.resolve();
    };
    const load = async (kind = "history") => {
      root.querySelector("input").value = "demo";
      root.querySelector("select").value = kind;
      root
        .querySelector("form")
        .dispatchEvent(new Event("submit", { cancelable: true }));
      await flush();
    };
    const advance = async (ms, refresh = true) => {
      wall += ms;
      mono += ms;
      view.tick(refresh);
      await flush();
    };
    await load();
    const details = root.querySelector(".observatory-output details");
    details.open = true;
    const initial = root.querySelector(".observatory-output").firstChild;
    for (let n = 0; n < 6; n++) await advance(6000);
    const renewed =
      calls.length >= 7 &&
      initial === root.querySelector(".observatory-output").firstChild &&
      details.isConnected &&
      details.open;
    if (!renewed) throw Error("unchanged_view_not_retained");
    reject = true;
    await advance(6000);
    if (root.querySelector(".observatory-output").textContent)
      throw Error("failure_did_not_clear");
    reject = false;
    await load();
    hold = true;
    await advance(6000);
    root.querySelector("input").value = "other";
    root.querySelector("input").dispatchEvent(new Event("input"));
    held();
    await flush();
    if (root.querySelector(".observatory-output").textContent)
      throw Error("late_reply_repainted");
    hold = false;
    await load();
    await advance(16000, false);
    if (root.querySelector(".observatory-output").textContent)
      throw Error("paused_lease_extended");
    await load();
    hold = true;
    await advance(6000);
    authority = { ...authority, authorization_epoch: 2 };
    view.tick();
    held();
    await flush();
    if (root.querySelector(".observatory-output").textContent)
      throw Error("old_authority_repainted");
    hold = false;
    await load("review");
    const composer = root.querySelector(
      'textarea[aria-label="Command arguments JSON"]',
    );
    if (!composer) throw Error("multi_read_view_not_loaded");
    composer.value = '{"draft":"keep this text"}';
    await advance(6000);
    if (!composer.isConnected || !composer.value.includes("keep this text"))
      throw Error("unchanged_multi_read_draft_lost");
    rejectAction = "constraints";
    await advance(6000);
    if (root.querySelector(".observatory-output").textContent)
      throw Error("one_read_renewed_other_denied_content");
    rejectAction = "";
    await load();
    wall -= 100;
    view.tick();
    if (root.querySelector(".observatory-output").textContent)
      throw Error("clock_rollback_retained");
    const readCalls = new Set([
      "inspection:history",
      "knowledge:query",
      "knowledge:constraints",
      "experience:query",
      "experience:ranking_inspect",
      "inspection:held",
      "inspection:trust_list",
    ]);
    const onlyReads = calls.every(
      ({ path, operation, action }) =>
        path === "/api/command" && readCalls.has(`${operation}:${action}`),
    );
    Date.now = originalWall;
    Object.defineProperty(performance, "now", {
      configurable: true,
      value: originalMono,
    });
    root.remove();
    return { renewed, onlyReads, calls };
  });
  assert.ok(result.renewed && result.onlyReads, JSON.stringify(result));
  checks.push(
    "compiled component: unchanged view/details retained beyond 36 seconds; failed renewal, paused expiry, scope/authority races and clock rollback clear; automatic calls are reads only",
  );
  await page.reload();
  const login = JSON.parse(await readFile(options.bootstrap_file, "utf8"));
  await page.locator("#code").fill(login.code);
  await page.locator("#login-form button").click();
  await page.locator("#logout").waitFor({ state: "visible" });
  assert.equal(
    await page.getByRole("button", { name: /^Motion (on|off)$/ }).count(),
    0,
  );
  await page.locator("#execution-project").selectOption("demo");
  await page.locator("#execution-form button").click();
  await page.locator(".task-card").first().waitFor();
  await page.screenshot({
    path: dirname(options.output) + "/dashboard.png",
    fullPage: false,
  });
  await page
    .locator("#execution-plans")
    .getByRole("button", { name: "View plan", exact: true })
    .click();
  const phase = page.locator(
    '#execution-plans details[data-task="plan-checkout-plan-design"]',
  );
  await phase.locator("summary").click();
  await page.waitForTimeout(11000);
  assert.equal(await phase.getAttribute("open"), "");
  assert.match(
    await page.locator("#execution-plans").innerText(),
    /50% · 1 of 2 steps completed/,
  );
  await page.locator("#execution-panel").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: dirname(options.output) + "/desktop-plans.png",
    fullPage: false,
  });
  await page
    .locator("#execution-plans")
    .getByRole("button", { name: "Back to plans", exact: true })
    .click();
  await page
    .locator("#execution-plans")
    .getByRole("button", { name: "View plan", exact: true })
    .waitFor();
  checks.push(
    "owner structured plans: canonical phase/step progress, detail and direct return, expanded phase retained through renewal",
  );
  const form = page.locator("#observatory > form");
  await form.locator("input").fill("demo");
  await form.getByRole("button", { name: "Load current view" }).click();
  const node = page.locator("#observatory .graph-list button").first();
  await node.waitFor();
  await node.click();
  const selected = page.locator(
    '#observatory [aria-label="Selected node or edge"]',
  );
  const content = await selected.innerText();
  let reads = 0;
  page.on("request", (r) => {
    if (
      r.url().endsWith("/api/command") &&
      r.postDataJSON()?.command?.action === "atlas"
    )
      reads++;
  });
  await page.waitForTimeout(32000);
  assert.ok(reads >= 5);
  assert.equal(await selected.innerText(), content);
  assert.ok((await form.locator("input").inputValue()) === "demo");
  assert.equal(await page.locator("#observatory .atlas-workspace").count(), 1);
  await page.screenshot({
    path: dirname(options.output) + "/memory-retained.png",
    fullPage: false,
  });
  checks.push(
    "real canonical SQLite dashboard: five or more renewals over 32 seconds retain selected graph content; no Motion control; screenshot updated",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  assert.equal(
    await page.locator("#observatory .observatory-output").textContent(),
    "",
  );
  assert.deepEqual(errors, []);
  await writeFile(
    options.output,
    JSON.stringify(
      {
        passed: true,
        browser: await browser.version(),
        scope:
          "Desktop Chrome/macOS; canonical SQLite plus explicit injected component faults, no physical device or shared-backend claim",
        checks,
        result,
        actual_atlas_refreshes: reads,
        page_errors: errors,
      },
      null,
      2,
    ) + "\n",
  );
} finally {
  await browser.close();
}
