// Browser-level product probes. Disposable canonical server; provider page only
// is controlled. Explicit races/clock/network faults are identified in results.
import assert from "node:assert/strict";
import { readFile, writeFile, rename } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";
import { request as httpsRequest } from "node:https";
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
const checks = [],
  errors = [],
  unexpected = [],
  traffic = [],
  assets = new Map(),
  pending = [];
async function fixtureResponse(route) {
  // Playwright's Node fetch does not use Chrome's host-resolver rules. Connect
  // explicitly to the disposable loopback server with its exact fixture CA.
  const headers = await route.request().allHeaders();
  return await new Promise((resolve, reject) => {
    const request = httpsRequest(
      {
        hostname: "127.0.0.1",
        port: new URL(options.origin).port,
        servername: "viewer.example.invalid",
        ca: options.certificate,
        path:
          new URL(route.request().url()).pathname +
          new URL(route.request().url()).search,
        headers,
        timeout: 5000,
      },
      (result) => {
        const parts = [];
        let bytes = 0;
        result.on("data", (part) => {
          bytes += part.length;
          if (bytes > 1048576)
            request.destroy(new Error("fixture_response_limit"));
          else parts.push(part);
        });
        result.on("end", () =>
          resolve({
            status: result.statusCode,
            headers: Object.fromEntries(
              Object.entries(result.headers).filter(
                ([key, value]) =>
                  typeof value === "string" &&
                  ![
                    "transfer-encoding",
                    "content-length",
                    "connection",
                  ].includes(key),
              ),
            ),
            body: Buffer.concat(parts),
          }),
        );
      },
    );
    request.on("error", () =>
      reject(new Error("fixture_loopback_request_failed")),
    );
    request.on("timeout", () => request.destroy());
    request.end();
  });
}
try {
  const context = await browser.newContext({
    viewport: { width: 412, height: 915 },
    reducedMotion: "reduce",
    colorScheme: "dark",
  });
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === options.origin || url.protocol === "blob:")
      return route.continue();
    if (
      url.origin === "https://github.com" &&
      url.pathname === "/login/oauth/authorize"
    ) {
      const query = Object.fromEntries(url.searchParams);
      assert.equal(query.scope, "");
      assert.equal(query.code_challenge_method, "S256");
      await writeFile(options.exchange, JSON.stringify(query));
      return route.fulfill({
        contentType: "text/html",
        body: `<a id="continue" href="${query.redirect_uri}?state=${query.state}&amp;code=controlled-code&amp;iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth">Continue</a>`,
      });
    }
    unexpected.push(url.origin + url.pathname);
    return route.abort();
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    traffic.push({ method: request.method(), path: url.pathname });
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (
      url.origin === options.origin &&
      !url.pathname.includes("/api/") &&
      !url.pathname.includes("/auth/")
    )
      pending.push(
        response
          .body()
          .then((raw) =>
            assets.set(url.pathname, {
              bytes: raw.length,
              gzip: gzipSync(raw).length,
            }),
          )
          .catch(() => {}),
      );
  });
  const wait = async (text) =>
    page.getByText(text, { exact: true }).first().waitFor({ timeout: 20000 });
  const tab = async (text) => {
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("button", { name: text, exact: true })
      .click();
  };
  async function login() {
    await page.goto(options.origin + "/viewer/");
    await page.getByRole("button", { name: "Continue with GitHub" }).click();
    await page.locator("#continue").click();
    await wait("Fix checkout retry handling");
  }
  await login();
  checks.push("UI sign-in with actual state/PKCE/session; controlled provider");
  assert.equal(
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("button")
      .count(),
    4,
  );
  for (const text of [
    "1 Passed",
    "1 Failed",
    "1 Not evaluated",
    "1 Waived",
    "1 Unknown",
    "Need a complete regression result",
  ])
    await wait(text);
  assert.equal(traffic.filter((r) => r.path === "/viewer/graph.js").length, 0);
  await page.screenshot({
    path: options.output + "/overview-412.png",
    fullPage: false,
  });
  await page.getByRole("button", { name: "MemoryKing", exact: true }).click();
  assert.equal(
    await page
      .locator(".brand")
      .evaluate((node) => node.classList.contains("orbit")),
    false,
  );
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "project");
  await page.getByRole("button", { name: "View task", exact: true }).focus();
  await page.keyboard.press("Enter");
  await wait("Acceptance criteria");
  assert.equal(await page.evaluate(() => document.activeElement.id), "main");
  await page.getByRole("button", { name: "Reports", exact: true }).click();
  await wait(
    "Showing reports linked to this task on the current catalog page. Other pages may contain more.",
  );
  await wait("Synthetic phase");
  await tab("Overview");
  await wait("Fix checkout retry handling");
  checks.push(
    "native keyboard navigation and focus, reduced-motion logo, task-linked report entry",
  );
  await page.locator("#project").selectOption("plans");
  await page.getByRole("button", { name: "Plans", exact: true }).click();
  await wait("Reliable checkout");
  await wait("50% · 1 of 2 steps completed");
  await page.getByRole("button", { name: "View plan", exact: true }).click();
  await wait("Inspect retry behavior");
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
  });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "";
  });
  await page.screenshot({
    path: options.output + "/structured-plan-412.png",
    fullPage: false,
  });
  await page
    .getByRole("button", { name: "View phase steps", exact: true })
    .last()
    .click();
  await wait("Steps · verify");
  await wait("Test duplicate retry");
  assert.equal(await page.locator(".plan-step").count(), 1);
  const planPolls = traffic.filter((r) =>
    r.path.includes("/plans/checkout-plan"),
  ).length;
  await page.waitForTimeout(11000);
  await wait("Steps · verify");
  assert.ok(
    traffic.filter((r) => r.path.includes("/plans/checkout-plan")).length >
      planPolls,
  );
  await page.getByRole("button", { name: "All steps", exact: true }).click();
  await page.getByRole("button", { name: "Evidence 1", exact: true }).click();
  await wait("Source: plan-regression");
  await page.getByRole("button", { name: "Back to plan", exact: true }).click();
  await wait("Inspect retry behavior");
  await page
    .getByRole("button", { name: "Back to plans", exact: true })
    .click();
  await wait("Reliable checkout");
  await tab("Tasks");
  await page
    .getByRole("button", { name: "Knowledge graph", exact: true })
    .click();
  await page.locator(".graph-node.task").click();
  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open task", exact: true })
    .click();
  await page
    .locator(".graph-drilldown")
    .getByRole("button", { name: "View plan", exact: true })
    .click();
  await page.locator(".graph-drilldown .plan").waitFor();
  await page
    .getByRole("button", { name: "Back to graph", exact: true })
    .click();
  await page.locator(".graph-viewport").waitFor();
  checks.push(
    "plan opened from graph retains a direct Back to graph with the same authorized graph",
  );
  await context.setOffline(true);
  await page.waitForTimeout(1000);
  assert.equal(await page.locator(".plan,.plan-step").count(), 0);
  await context.setOffline(false);
  await page.goto(options.origin + "/viewer/");
  await wait("Fix checkout retry handling");
  checks.push(
    "structured plans: equal-step progress, phase filtering retained through renewal, current evidence and direct return, list navigation and offline clearing",
  );
  const layout = [];
  for (const width of [360, 412]) {
    await page.setViewportSize({ width, height: 915 });
    for (const zoom of [1, 2]) {
      await page.evaluate((zoom) => {
        document.documentElement.style.fontSize = `${zoom * 100}%`;
      }, zoom);
      const result = await page.evaluate(() => ({
        width: innerWidth,
        content: document.documentElement.scrollWidth,
        targets: [
          ...document.querySelectorAll(
            "button,input,select,a.external,summary",
          ),
        ].map((node) => ({
          label: node.textContent,
          width: node.getBoundingClientRect().width,
          height: node.getBoundingClientRect().height,
        })),
      }));
      assert.ok(result.content <= width, JSON.stringify(result));
      assert.ok(
        result.targets.every((node) => node.height >= 44 && node.width >= 44),
        JSON.stringify(result.targets),
      );
      layout.push({
        width,
        text_scale: zoom,
        no_page_overflow: true,
        targets_44px: true,
      });
    }
  }
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "";
  });
  await page.setViewportSize({ width: 412, height: 915 });
  await tab("Tasks");
  await page.getByRole("button", { name: "View task", exact: true }).click();
  await wait("Acceptance criteria");
  await page
    .getByRole("button", { name: "View evidence", exact: true })
    .first()
    .click();
  await wait("Source: synthetic-regression-run");
  assert.match(
    await page.locator("main").innerText(),
    /<script>window.PWNED=1<\/script>/,
  );
  assert.equal(await page.evaluate(() => window.PWNED), undefined);
  assert.equal(
    await page
      .locator("main script, main iframe, main img[src*='tracker']")
      .count(),
    0,
  );
  for (const link of await page.locator("a.external").evaluateAll((links) =>
    links.map((link) => ({
      rel: link.rel,
      referrer: link.referrerPolicy,
      href: link.href,
    })),
  )) {
    assert.equal(link.rel, "noopener noreferrer");
    assert.equal(link.referrer, "no-referrer");
    assert.ok(link.href.startsWith("https://"));
  }
  await tab("Memory");
  await page.getByLabel("Search memory").fill("NO_MATCHING_SYNTHETIC_TERM");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await wait("No matching memories are available in this view.");
  await tab("Settings");
  await wait("Phone installation");
  await page.getByLabel("Color theme").selectOption("light");
  assert.equal(
    await page.evaluate(() => localStorage.getItem("memoryking-viewer-theme")),
    "light",
  );
  const contrast = [];
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Color theme").selectOption(theme);
    contrast.push(
      await page.evaluate((theme) => {
        const styles = getComputedStyle(document.documentElement);
        const luminance = (name) => {
          let hex = styles.getPropertyValue(name).trim().slice(1);
          if (hex.length === 3)
            hex = [...hex].map((digit) => digit + digit).join("");
          const channels = [0, 2, 4]
            .map((start) => parseInt(hex.slice(start, start + 2), 16) / 255)
            .map((x) =>
              x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4,
            );
          return (
            channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
          );
        };
        const pairs = [];
        for (const fg of [
          "--text",
          "--muted",
          "--accent",
          "--good",
          "--bad",
          "--pending",
        ])
          for (const bg of ["--bg", "--panel", "--panel-alt"]) {
            const a = luminance(fg),
              b = luminance(bg);
            pairs.push({
              fg,
              bg,
              ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
            });
          }
        return { theme, pairs };
      }, theme),
    );
  }
  assert.ok(
    contrast.every((theme) => theme.pairs.every((pair) => pair.ratio >= 4.5)),
    JSON.stringify(contrast),
  );
  await page.getByLabel("Color theme").selectOption("dark");
  const priorGraphAssets = traffic.filter(
    (r) => r.path === "/viewer/graph.js",
  ).length;
  await tab("Memory");
  await page
    .getByRole("button", { name: "Knowledge graph", exact: true })
    .click();
  await page.locator(".graph-viewport").waitFor();
  assert.equal(await page.locator(".graph-node").count(), 9);
  assert.equal(await page.locator(".relationships li").count(), 9);
  assert.equal(
    traffic.filter((r) => r.path === "/viewer/graph.js").length,
    priorGraphAssets + 1,
  );
  await page.locator(".graph-node.claim").click();
  assert.ok(
    (await page.locator(".graph-detail").textContent()).includes("cites:"),
  );
  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open memory", exact: true })
    .click();
  await wait("Source: accepted_knowledge");
  await page
    .getByRole("button", { name: "Back to graph", exact: true })
    .click();
  await page.locator(".graph-viewport").waitFor();
  const memoryNode = page
    .locator(".graph-node.observation")
    .filter({ hasText: "Synthetic finding" });
  await memoryNode.click();
  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open memory", exact: true })
    .waitFor();
  assert.ok(
    (await page.locator(".graph-detail").textContent()).includes("used by"),
  );
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  assert.equal(await page.getByLabel("Graph zoom").textContent(), "130%");
  const retainedGraph = await page.locator(".graph-viewport").elementHandle();
  const previousGraphReads = traffic.filter((r) =>
    r.path.endsWith("/graph"),
  ).length;
  await page.waitForFunction(
    () => document.querySelector(".graph-node") !== null,
  );
  for (
    let attempt = 0;
    attempt < 140 &&
    traffic.filter((r) => r.path.endsWith("/graph")).length ===
      previousGraphReads;
    attempt++
  )
    await page.waitForTimeout(100);
  assert.ok(
    traffic.filter((r) => r.path.endsWith("/graph")).length >
      previousGraphReads,
  );
  assert.equal(await retainedGraph.evaluate((n) => n.isConnected), true);
  assert.equal(await page.getByLabel("Graph zoom").textContent(), "130%");
  const graphBox = await page.locator(".graph-viewport").boundingBox();
  const beforeDrag = await memoryNode.getAttribute("style");
  const nodeBox = await memoryNode.boundingBox();
  await page.mouse.move(nodeBox.x + 22, nodeBox.y + 22);
  await page.mouse.down();
  await page.mouse.move(nodeBox.x + 48, nodeBox.y + 40, { steps: 4 });
  await page.mouse.up();
  assert.notEqual(await memoryNode.getAttribute("style"), beforeDrag);
  await page.mouse.move(graphBox.x + 8, graphBox.y + 8);
  await page.mouse.down();
  await page.mouse.move(graphBox.x + 35, graphBox.y + 25, { steps: 4 });
  await page.mouse.up();
  const cdp = await context.newCDPSession(page);
  const cx = graphBox.x + graphBox.width / 2,
    cy = graphBox.y + graphBox.height / 2;
  const beforePinch = Number(
    await page.locator(".graph-viewport").getAttribute("data-zoom"),
  );
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [
      { x: cx - 30, y: cy, id: 1 },
      { x: cx + 30, y: cy, id: 2 },
    ],
  });
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchMove",
    touchPoints: [
      { x: cx - 60, y: cy, id: 1 },
      { x: cx + 60, y: cy, id: 2 },
    ],
  });
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  assert.ok(
    Number(await page.locator(".graph-viewport").getAttribute("data-zoom")) >
      beforePinch,
  );
  await cdp.detach();
  await page.getByRole("button", { name: "Labels", exact: true }).click();
  const returnZoom = await page.getByLabel("Graph zoom").textContent();
  const returnStyle = await memoryNode.getAttribute("style");
  assert.ok((await memoryNode.boundingBox()).width >= 44);

  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open memory", exact: true })
    .click();
  await page
    .locator(".source-text p")
    .filter({ hasText: options.marker })
    .first()
    .waitFor();
  assert.equal(await retainedGraph.evaluate((n) => n.isConnected), true);
  assert.equal(await page.locator(".graph-viewport").isVisible(), false);
  const detailReads = traffic.filter((r) =>
    r.path.includes("/memories/"),
  ).length;
  for (
    let attempt = 0;
    attempt < 140 &&
    traffic.filter((r) => r.path.includes("/memories/")).length === detailReads;
    attempt++
  )
    await page.waitForTimeout(100);
  assert.ok(
    traffic.filter((r) => r.path.includes("/memories/")).length > detailReads,
  );
  await page
    .getByRole("button", { name: "Back to graph", exact: true })
    .click();
  await page.locator(".graph-viewport").waitFor();
  assert.equal(await retainedGraph.evaluate((n) => n.isConnected), true);
  assert.equal(await page.getByLabel("Graph zoom").textContent(), returnZoom);
  assert.equal(await memoryNode.getAttribute("style"), returnStyle);
  assert.equal(await memoryNode.getAttribute("aria-pressed"), "true");
  assert.equal(
    await page
      .getByRole("button", { name: "Labels", exact: true })
      .getAttribute("aria-pressed"),
    "true",
  );
  await page.getByRole("button", { name: "Reset view", exact: true }).click();
  assert.equal(await page.getByLabel("Graph zoom").textContent(), "100%");
  await page.getByRole("button", { name: "Labels", exact: true }).click();
  await page.locator(".relationships summary").click();
  await page.getByLabel("Find in this view").fill("Fix checkout");
  assert.equal(await page.locator(".relationships li button").count(), 1);
  await page.locator(".relationships li button").focus();
  await page.keyboard.press("Enter");
  await page.locator(".relationships summary").click();
  await page.evaluate(() => {
    document.activeElement?.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: options.output + "/knowledge-graph.png",
    fullPage: false,
  });
  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open task", exact: true })
    .click();
  await page
    .locator(".graph-drilldown")
    .getByText("Retry regression recorded", { exact: true })
    .waitFor();
  await page
    .getByRole("button", { name: "View evidence", exact: true })
    .first()
    .click();
  await page.locator(".graph-drilldown .source-text").first().waitFor();
  await page
    .getByRole("button", { name: "Back to graph", exact: true })
    .click();
  await page.locator(".graph-viewport").waitFor();
  assert.equal(
    await page.getByLabel("Find in this view").inputValue(),
    "Fix checkout",
  );
  await page
    .locator(".graph-detail")
    .getByRole("button", { name: "Open task", exact: true })
    .click();
  await page
    .locator(".graph-drilldown")
    .getByText("Retry regression recorded", { exact: true })
    .waitFor();
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  assert.equal(
    await page
      .locator(".graph-node,.graph-detail,.relationships,.graph-drilldown")
      .count(),
    0,
  );
  await page.reload();
  await wait("Fix checkout retry handling");
  checks.push(
    "knowledge graph: actual permitted memories and evidence edges, touch pinch/pan/drag, selection/current-target detail, retained layout after renewal and Back to graph from memory/task/evidence detail, both-view authority renewal, keyboard list,44px targets and offline clearing; browser gesture synthesis is not physical Android qualification",
  );
  await tab("Overview");
  await page.getByRole("button", { name: "Reports", exact: true }).click();
  await page.getByRole("button", { name: "Read report", exact: true }).click();
  await wait("Full document loaded.");
  assert.equal(
    await page.locator(".report-body pre code").textContent(),
    options.code,
  );
  const retainedBody = await page.locator(".report-body").elementHandle();
  const priorMetadataReads = traffic.filter((r) =>
    r.path.endsWith("/mobile-report/versions/1"),
  ).length;
  await page.locator(".report-body pre code").evaluate((node) => {
    const selection = window.getSelection();
    selection.removeAllRanges();
    const range = document.createRange();
    range.selectNodeContents(node);
    selection.addRange(range);
  });
  await page.waitForTimeout(10300);
  assert.equal(
    await retainedBody.evaluate(
      (node) => node === document.querySelector(".report-body"),
    ),
    true,
  );
  assert.equal(
    await page.evaluate(() => window.getSelection().toString()),
    options.code,
  );
  assert.ok(
    traffic.filter((r) => r.path.endsWith("/mobile-report/versions/1")).length >
      priorMetadataReads,
  );
  await page.evaluate(() => window.getSelection().removeAllRanges());
  checks.push(
    "whole-report polling renews authority without replacing unchanged reader DOM or selected code",
  );
  for (const block of options.model.blocks) {
    if (block.kind === "heading")
      await page
        .locator(".report-body")
        .getByRole("heading", { name: block.text, exact: true })
        .waitFor();
    if (block.kind === "paragraph" || block.kind === "item")
      assert.ok(
        (await page.locator(".report-body").innerText()).includes(block.text),
      );
  }
  assert.equal(
    await page
      .locator(".report-body td")
      .allTextContents()
      .then((cells) => cells.join("|")),
    "unknown|New proof required",
  );
  for (const width of [360, 412]) {
    await page.setViewportSize({ width, height: 915 });
    assert.equal(await page.locator(".report-body table").isVisible(), false);
    assert.equal(await page.locator(".report-records").isVisible(), true);
    assert.deepEqual(
      await page.locator(".report-record dt").allTextContents(),
      options.model.blocks.find((block) => block.kind === "table").headers,
    );
    assert.deepEqual(
      await page.locator(".report-record dd").allTextContents(),
      ["unknown", "New proof required"],
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
    );
  }
  await page.setViewportSize({ width: 820, height: 915 });
  assert.equal(await page.locator(".report-body table").isVisible(), true);
  assert.equal(await page.locator(".report-records").isVisible(), false);
  await page.setViewportSize({ width: 412, height: 915 });
  checks.push(
    "report tables reflow into native labeled records at phone widths; wider views retain semantic tables",
  );
  await page.getByRole("button", { name: "Load figure", exact: true }).click();
  await page.locator("figure img").waitFor();
  assert.ok(
    await page
      .locator("figure img")
      .evaluate((image) => image.complete && image.naturalWidth === 8),
  );
  assert.equal(traffic.filter((r) => /\.(pdf|epub)$/.test(r.path)).length, 0);
  await page.screenshot({
    path: options.output + "/report-reader.png",
    fullPage: false,
  });
  checks.push(
    "complete long report, >64 fragments, full headings/code/table and hash-verified figure; no PDF/EPUB download",
  );
  // BFcache/visibility signals are injected; the real page's listeners and DOM
  // are inspected. Physical Android lifecycle remains a separate qualification.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  assert.equal(
    await page.locator(".report-body, figure img, nav.tabs, #project").count(),
    0,
  );
  assert.ok(
    !(await page.locator("body").innerText()).includes("Synthetic phase"),
  );
  const pausedCount = traffic.length;
  await page.waitForTimeout(1100);
  assert.equal(traffic.length, pausedCount);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    );
  });
  await wait("Fix checkout retry handling");
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
  });
  assert.equal(await page.locator("#project").count(), 0);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("online"));
  });
  await wait("Fix checkout retry handling");
  await page.locator("#project").selectOption("q");
  await wait("No visible task checkpoints in this view.");
  assert.ok(!(await page.locator("main").innerText()).includes(options.marker));
  await page.locator("#project").selectOption("p");
  await wait("Fix checkout retry handling");
  checks.push(
    "background and BFcache clear private DOM/figures, visible resume rechecks, offline clears, project switch starts fresh",
  );
  // Hold a real data response while the presentation is cleared, then deliver it.
  let release, enteredResolve;
  const held = new Promise((r) => {
      release = r;
    }),
    entered = new Promise((r) => {
      enteredResolve = r;
    });
  await page.route("**/viewer/api/v1/projects/p/tasks", async (route) => {
    const response = await fixtureResponse(route);
    enteredResolve();
    await held;
    await route.fulfill(response).catch(() => {});
  });
  await tab("Tasks");
  await entered;
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
  });
  release();
  await page.waitForTimeout(200);
  assert.equal(await page.locator("nav.tabs").count(), 0);
  assert.ok(
    !(await page.locator("main").innerText()).includes(
      "Fix checkout retry handling",
    ),
  );
  await page.unroute("**/viewer/api/v1/projects/p/tasks");
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await wait("Fix checkout retry handling");
  checks.push("real held HTTP response cannot repaint after clearing");
  // Suspend effective freshness without running its expiry timeout; first input
  // must synchronously erase before navigation or any private action.
  await page.evaluate(() => {
    const mono = performance.now.bind(performance),
      wall = Date.now;
    performance.now = () => mono() + 16000;
    Date.now = () => wall() + 16000;
    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  assert.equal(await page.locator("nav.tabs").count(), 0);
  await page.reload();
  await wait("Fix checkout retry handling");
  await page.evaluate(() => {
    const now = Date.now;
    Date.now = () => now() - 60000;
    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  assert.equal(await page.locator("#project").count(), 0);
  await page.reload();
  await wait("Fix checkout retry handling");
  checks.push(
    "timer-independent expiry and wall rollback clear before a private input",
  );
  for (const status of [429, 503]) {
    let rejected = 0;
    const target = "**/viewer/api/v1/session";
    await page.route(target, (route) => {
      rejected++;
      return route.fulfill({
        status,
        contentType: "application/json",
        headers: { "Retry-After": "120", "Cache-Control": "no-store" },
        body: JSON.stringify({
          schema_version: 1,
          error: status === 429 ? "rate_limited" : "service_unavailable",
        }),
      });
    });
    await page.evaluate(() =>
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: true }),
      ),
    );
    await wait(status === 429 ? "Request limit" : "Unavailable");
    assert.equal(await page.locator("#project,nav.tabs").count(), 0);
    assert.equal(
      await page.getByRole("button", { name: "Continue with GitHub" }).count(),
      0,
    );
    await page.getByRole("button", { name: "Check connection" }).click();
    await page.waitForTimeout(100);
    await page.evaluate(() => {
      window.dispatchEvent(new Event("offline"));
      window.dispatchEvent(new Event("online"));
    });
    await page.waitForTimeout(100);
    assert.equal(rejected, 1);
    await page.unroute(target);
    await page.reload();
    await wait("Fix checkout retry handling");
  }
  checks.push(
    "429/503 clear neutrally, Retry-After blocks manual and lifecycle retry loops",
  );
  await tab("Settings");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await wait("Signed out.");
  assert.equal(await page.locator("nav.tabs, #project").count(), 0);
  const storage = await page.evaluate(async () => ({
    local: Object.fromEntries(Object.entries(localStorage)),
    session: Object.fromEntries(Object.entries(sessionStorage)),
    databases: await indexedDB.databases(),
    caches: await caches.keys(),
    serviceWorkers: (await navigator.serviceWorker.getRegistrations()).length,
    cookie: document.cookie,
  }));
  assert.deepEqual(storage.local, { "memoryking-viewer-theme": "dark" });
  assert.deepEqual(storage.session, {});
  assert.deepEqual(storage.databases, []);
  assert.deepEqual(storage.caches, []);
  assert.equal(storage.serviceWorkers, 0);
  assert.equal(storage.cookie, "");
  // A real owner reduction reaches the canonical store, while an authorized
  // response from the previous binding is held in transit. Refresh discovers
  // revocation before the delayed response is released to the old client cycle.
  await login();
  let releaseRevoked, enteredRevoked;
  const holdRevoked = new Promise((resolve) => {
    releaseRevoked = resolve;
  });
  const sawRevoked = new Promise((resolve) => {
    enteredRevoked = resolve;
  });
  await page.route("**/viewer/api/v1/projects/p/tasks", async (route) => {
    const response = await fixtureResponse(route);
    enteredRevoked();
    await holdRevoked;
    await route.fulfill(response).catch(() => {});
  });
  await tab("Tasks");
  await sawRevoked;
  await writeFile(
    options.operator_command + ".tmp",
    JSON.stringify({ action: "disable", id: 1 }),
  );
  await rename(options.operator_command + ".tmp", options.operator_command);
  const acknowledgement = async () => {
    try {
      return JSON.parse(await readFile(options.operator_ack, "utf8"));
    } catch {
      return null;
    }
  };
  let ack = null;
  for (let attempt = 0; attempt < 200 && !ack; attempt++) {
    ack = await acknowledgement();
    if (!ack) await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.deepEqual(ack, { id: 1, completed: true });
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    ),
  );
  await page
    .getByRole("button", { name: "Continue with GitHub", exact: true })
    .waitFor();
  releaseRevoked();
  await page.waitForTimeout(200);
  assert.equal(
    await page.locator("nav.tabs,#project,.task,.report-body").count(),
    0,
  );
  assert.ok(
    !(await page.locator("body").innerText()).includes(
      "Fix checkout retry handling",
    ),
  );
  await page.unroute("**/viewer/api/v1/projects/p/tasks");
  checks.push(
    "real canonical owner disable and current-context recheck reject a delayed old-authority response",
  );
  await context.setOffline(true);
  await page.reload().catch(() => {});
  assert.ok(!(await page.locator("body").innerText()).includes(options.marker));
  checks.push(
    "logout clears, appearance-only storage, no service worker/database/cache, cold offline has no private content",
  );
  await Promise.all(pending);
  const critical = [...assets].filter(
    ([path]) => !path.endsWith("/graph.js") && !path.endsWith("/icon-512.png"),
  );
  const compressed = critical.reduce(
    (total, [, asset]) => total + asset.gzip,
    0,
  );
  const rawTransfer = critical.reduce(
    (total, [, asset]) => total + asset.bytes,
    0,
  );
  assert.ok(rawTransfer <= 750 * 1024);
  assert.deepEqual(unexpected, []);
  assert.deepEqual(errors, []);
  await writeFile(
    options.output + "/result.json",
    JSON.stringify(
      {
        passed: true,
        browser: await browser.version(),
        viewport: "desktop Chromium 360/412 CSS-pixel emulation",
        checks,
        layout,
        contrast,
        storage,
        critical_raw_entity_bytes: rawTransfer,
        critical_gzip_estimate_bytes: compressed,
        transfer_note:
          "Server sent uncompressed bytes. Gzip is an estimate only; observed raw entities already meet the budget.",
        asset_bytes: Object.fromEntries(assets),
        requests: traffic,
        unexpected_network: unexpected,
        page_errors: errors,
      },
      null,
      2,
    ) + "\n",
  );
} catch (error) {
  await writeFile(
    options.output + "/failure.json",
    JSON.stringify(
      { error: error.stack, checks, errors, unexpected, traffic },
      null,
      2,
    ) + "\n",
  );
  throw error;
} finally {
  await browser.close();
}
