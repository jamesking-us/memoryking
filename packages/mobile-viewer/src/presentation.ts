import type {
  ViewerTask,
  ViewerMemory,
  ViewerReportItem,
  ViewerReportData,
} from "@memoryking/contracts";
import {
  ViewerClient,
  type State,
  type Selection,
  type Tab,
} from "./client.js";
import { safeLink, type Block } from "./report.js";

const el = <K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text = "",
  className = "",
): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
};
const pill = (text: string, state = "") => el("span", text, `pill ${state}`);
const title = (text: string, detail: string) => {
  const header = el("header", "", "section-header");
  header.append(el("h1", text), el("p", detail));
  return header;
};
const labels: Record<Tab, string> = {
  overview: "Overview",
  tasks: "Tasks",
  memories: "Memory",
  settings: "Settings",
};
const states: Record<string, string> = {
  pending: "Not evaluated",
  passed: "Passed",
  failed: "Failed",
  waived: "Waived",
  unknown: "Unknown",
};

export class Presentation {
  client!: ViewerClient;
  private lastReadyGeneration = -1;
  private lastReportKey: string | null = null;
  private contentsOpen = false;
  constructor(readonly root: HTMLElement) {}
  private action(
    text: string,
    key: string,
    callback: () => void,
    primary = false,
  ): HTMLButtonElement {
    const button = el("button", text, primary ? "button primary" : "button");
    button.type = "button";
    button.dataset.focus = key;
    button.addEventListener("click", () => {
      if (this.client.check()) callback();
    });
    return button;
  }
  private open(
    text: string,
    key: string,
    selection: Selection,
  ): HTMLButtonElement {
    return this.action(text, key, () => this.client.select(selection));
  }
  private paragraph(text: string): HTMLElement {
    const box = el("div", "", "source-text");
    box.append(el("p", text));
    const urls = [
      ...new Set(text.match(/https?:\/\/[^\s<>"`\[\]()]+/g) ?? []),
    ].slice(0, 20);
    for (const candidate of urls) {
      const url = safeLink(candidate);
      if (!url) continue;
      const link = el("a", `Open link: ${new URL(url).hostname}`, "external");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      link.addEventListener("click", (event) => {
        if (!this.client.check()) event.preventDefault();
      });
      box.append(link);
    }
    return box;
  }
  render(state: State): void {
    if (state.status === "ready" && !this.client.check()) return;
    const reportKey =
      state.status === "ready" &&
      state.response?.data.kind === "report" &&
      state.report?.complete
        ? JSON.stringify({
            generation: this.client.gate.generation,
            project: state.project,
            selection: state.selection,
            metadata: state.response.data,
            blocks: state.report.blocks.length,
            figures: [...state.figures],
          })
        : null;
    // The controller has renewed the whole immutable report's authority. Keep
    // unchanged DOM so polling does not destroy text selection or reading position.
    if (
      reportKey &&
      reportKey === this.lastReportKey &&
      this.root.querySelector(".report-body")
    )
      return;
    this.lastReportKey = reportKey;
    this.contentsOpen =
      state.status === "ready" &&
      this.lastReadyGeneration === this.client.gate.generation
        ? (this.root.querySelector<HTMLDetailsElement>(".contents")?.open ??
          false)
        : false;
    const focused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement.dataset.focus
        : undefined;
    const searchDraft =
      state.status === "ready"
        ? this.root.querySelector<HTMLInputElement>("#memory-search")?.value
        : undefined;
    this.root.replaceChildren();
    const top = el("header", "", "topbar");
    const brand = el("button", "", "brand");
    brand.type = "button";
    brand.setAttribute("aria-label", "MemoryKing");
    brand.dataset.focus = "brand";
    const logo = el("img");
    logo.src = "/viewer/elephant.png";
    logo.width = 56;
    logo.height = 56;
    logo.alt = "";
    brand.append(logo, el("span", "MemoryKing"));
    brand.addEventListener("click", () => {
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
        brand.classList.remove("orbit");
        void brand.offsetWidth;
        brand.classList.add("orbit");
      }
    });
    top.append(brand, pill("Viewer", "muted"));
    this.root.append(top);
    const main = el("main");
    main.id = "main";
    main.dataset.focus = "main";
    main.tabIndex = -1;
    if (state.status !== "ready") {
      main.append(
        title(
          "Project dashboard",
          "Read-only access to saved tasks, memory and reports.",
        ),
      );
      const panel = el("section", "", "panel neutral");
      panel.setAttribute("role", "status");
      panel.append(
        el(
          "h2",
          state.status === "signed_out"
            ? "Sign in"
            : state.status === "loading"
              ? "Loading"
              : state.status === "paused"
                ? "Paused"
                : state.status === "limited"
                  ? "Request limit"
                  : "Unavailable",
        ),
        el("p", state.message),
      );
      if (state.status === "signed_out") {
        const login = el("button", "Continue with GitHub", "button primary");
        login.type = "button";
        login.addEventListener("click", () => {
          login.disabled = true;
          void this.client.login().then((url) => {
            if (url) location.assign(url);
          });
        });
        panel.append(login);
        panel.append(
          el(
            "p",
            "Your operator must grant access to each project. A GitHub account alone does not share project content.",
            "muted",
          ),
        );
      } else if (!["loading", "paused"].includes(state.status)) {
        const retry = el("button", "Check connection", "button");
        retry.type = "button";
        retry.addEventListener("click", () => {
          void this.client.refresh();
        });
        panel.append(retry);
      }
      main.append(panel);
      this.root.append(main);
      return;
    }
    const session = state.session?.data;
    if (session?.kind !== "session") return;
    const context = el("section", "", "project-context");
    const label = el("label", "Project");
    label.htmlFor = "project";
    const select = el("select");
    select.id = "project";
    select.dataset.focus = "project";
    for (const project of session.projects) {
      const option = el("option", project);
      option.value = project;
      option.selected = project === state.project;
      select.append(option);
    }
    select.addEventListener("change", () =>
      this.client.select({ tab: "overview", kind: "overview" }, select.value),
    );
    context.append(label, select);
    if (!session.projects.length)
      context.append(
        el("p", "No projects are currently shared with this account."),
      );
    main.append(context);
    const data = state.response?.data,
      selection = state.selection;
    const heading =
      selection.kind === "report"
        ? "Report"
        : selection.kind === "reports"
          ? "Reports"
          : selection.kind === "graph"
            ? "Relationships"
            : labels[selection.tab];
    main.append(
      title(
        heading,
        selection.kind === "report"
          ? "Reviewed document and delivery status."
          : selection.kind === "reports"
            ? "Published reports you have permission to read."
            : selection.kind === "graph"
              ? "Visible tasks and their declared criteria."
              : selection.tab === "overview"
                ? "Saved progress and evidence for this project."
                : selection.tab === "tasks"
                  ? "Agent-reported state, criteria and blockers."
                  : selection.tab === "memories"
                    ? "Source-attributed findings shared with you."
                    : "Session, display and installation.",
      ),
    );
    const toolbar = el("div", "", "toolbar");
    if (selection.kind !== selection.tab)
      toolbar.append(
        this.open(`Back to ${labels[selection.tab]}`, "back", {
          tab: selection.tab,
          kind: selection.tab,
        }),
      );
    if (selection.kind === "overview" || selection.kind === "task")
      toolbar.append(
        this.open("Reports", "reports", {
          tab: selection.tab,
          kind: "reports",
          ...(selection.kind === "task" ? { task: selection.id } : {}),
        }),
      );
    if (selection.kind === "tasks")
      toolbar.append(
        this.open("Relationships", "graph", { tab: "tasks", kind: "graph" }),
      );
    if (selection.kind === "memories") {
      const form = el("form", "", "search");
      const searchLabel = el("label", "Search memory");
      searchLabel.htmlFor = "memory-search";
      const input = el("input");
      input.type = "search";
      input.id = "memory-search";
      input.dataset.focus = "search";
      input.maxLength = 256;
      input.autocomplete = "off";
      input.value = searchDraft ?? selection.query ?? "";
      const button = el("button", "Search", "button");
      button.type = "submit";
      form.append(searchLabel, input, button);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (this.client.check())
          this.client.select({
            tab: "memories",
            kind: "memories",
            query: input.value,
          });
      });
      toolbar.append(form);
    }
    main.append(toolbar);
    if (selection.kind === "settings") this.settings(main, state);
    else if (
      data?.kind === "overview" ||
      data?.kind === "tasks" ||
      data?.kind === "task"
    ) {
      if (data.kind === "overview") {
        const metrics = el("section", "", "metrics");
        metrics.setAttribute("aria-label", "This bounded view");
        for (const [name, value] of [
          ["Shown tasks", data.tasks.length],
          [
            "With blockers",
            data.tasks.filter((task) => task.blockers.length > 0).length,
          ],
        ]) {
          const card = el("div", "", "metric");
          card.append(el("strong", String(value)), el("span", String(name)));
          metrics.append(card);
        }
        main.append(
          metrics,
          el(
            "p",
            "Counts cover only the tasks shown. Saved checkpoints are not proof of live agent activity.",
            "muted",
          ),
        );
      }
      if (!data.tasks.length)
        main.append(
          el("p", "No visible task checkpoints in this view.", "empty"),
        );
      for (const task of data.tasks)
        main.append(this.task(task, data.kind === "task"));
    } else if (data?.kind === "memories" || data?.kind === "memory") {
      if (!data.memories.length)
        main.append(
          el("p", "No matching memories are available in this view.", "empty"),
        );
      for (const memory of data.memories)
        main.append(this.memory(memory, data.kind === "memory"));
    } else if (data?.kind === "reports") {
      const reports = data.reports.filter(
        (report) => !selection.task || report.task === selection.task,
      );
      if (selection.task)
        main.append(
          el(
            "p",
            "Showing reports linked to this task on the current catalog page. Other pages may contain more.",
            "muted",
          ),
        );
      if (!reports.length)
        main.append(
          el("p", "No matching published reports on this page.", "empty"),
        );
      for (const report of reports) {
        const card = el("article", "", "panel");
        card.append(
          el("h2", report.title),
          this.channel(report),
          this.open(
            "Read report",
            `report-${report.report}-${report.revision}`,
            {
              tab: selection.tab,
              kind: "report",
              id: report.report,
              revision: report.revision,
            },
          ),
        );
        main.append(card);
      }
    } else if (data?.kind === "report") this.reader(main, data, state);
    else if (data?.kind === "graph") {
      const canvas = el("div", "", "graph");
      canvas.setAttribute("aria-hidden", "true");
      main.append(canvas);
      const generation = this.client.gate.generation,
        sequence = this.client.gate.sequence;
      void import("./graph.js")
        .then(({ drawGraph }) => {
          if (
            generation === this.client.gate.generation &&
            sequence === this.client.gate.sequence &&
            this.client.check() &&
            canvas.isConnected
          )
            drawGraph(canvas, data);
        })
        .catch(() => {
          /* The equivalent list remains usable without visualization. */
        });
      const list = el("ul", "", "relationships");
      for (const node of data.nodes) {
        const item = el(
          "li",
          `${node.kind === "task" ? "Task" : "Criterion"}: ${node.label}`,
        );
        const children = data.edges
          .filter((edge) => edge.source === node.id)
          .map((edge) => data.nodes.find((n) => n.id === edge.target)?.label)
          .filter(Boolean);
        if (children.length)
          item.append(el("p", `Declares: ${children.join("; ")}`, "muted"));
        list.append(item);
      }
      main.append(el("h2", "Relationship list"), list);
    }
    if (state.response) {
      const coverage = el("details", "", "coverage");
      coverage.append(
        el(
          "summary",
          state.response.truncated
            ? "Partial view · more content may be available"
            : "View coverage",
        ),
        el("p", state.response.coverage.replaceAll("_", " ")),
        el(
          "p",
          `Observed position ${state.response.binding.observed_position}. Refreshed while visible; access is checked before display.`,
        ),
      );
      main.append(coverage);
      if (
        state.response.next_cursor &&
        ["tasks", "memories", "reports"].includes(selection.kind)
      )
        main.append(
          this.open("Next page", "next", {
            ...selection,
            cursor: state.response.next_cursor,
          }),
        );
      if (selection.cursor)
        main.append(
          this.open("First page", "first", { ...selection, cursor: undefined }),
        );
    }
    const nav = el("nav", "", "tabs");
    nav.setAttribute("aria-label", "Main navigation");
    for (const tab of Object.keys(labels) as Tab[]) {
      const button = this.open(labels[tab], `tab-${tab}`, { tab, kind: tab });
      if (tab === selection.tab) button.setAttribute("aria-current", "page");
      nav.append(button);
    }
    this.root.append(main, nav);
    if (this.lastReadyGeneration !== this.client.gate.generation) {
      this.lastReadyGeneration = this.client.gate.generation;
      main.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    } else if (focused)
      [...this.root.querySelectorAll<HTMLElement>("[data-focus]")]
        .find((node) => node.dataset.focus === focused)
        ?.focus({ preventScroll: true });
  }
  private task(task: ViewerTask, detail: boolean): HTMLElement {
    const card = el("article", "", "panel task");
    const row = el("div", "", "card-heading");
    row.append(
      el(detail ? "h2" : "h3", task.objective),
      pill(
        task.blockers.length ? "Blocked" : "Checkpoint",
        task.blockers.length ? "failed" : "muted",
      ),
    );
    card.append(
      row,
      el("p", `Revision ${task.checkpoint_revision}`, "eyebrow"),
      this.paragraph(task.current_state),
    );
    const progress = el("div", "", "criteria-counts");
    for (const key of [
      "passed",
      "failed",
      "pending",
      "waived",
      "unknown",
    ] as const)
      progress.append(pill(`${task.progress[key]} ${states[key]}`, key));
    card.append(
      progress,
      el(
        "p",
        `${task.progress.total} declared criteria. Counts reflect the available evidence.`,
        "muted",
      ),
    );
    if (task.blockers.length) {
      const list = el("ul", "", "blockers");
      for (const text of task.blockers) list.append(el("li", text));
      card.append(el("h3", "Blockers"), list);
    }
    if (detail) {
      card.append(el("h3", "Acceptance criteria"));
      for (const criterion of task.criteria) {
        const item = el("section", "", "criterion");
        item.append(
          pill(states[criterion.state], criterion.state),
          this.paragraph(criterion.text),
        );
        for (const evidence of criterion.evidence)
          item.append(
            this.open("View evidence", `evidence-${evidence.handle}`, {
              tab: "memories",
              kind: "memory",
              id: evidence.handle,
            }),
          );
        if (!criterion.evidence_available)
          item.append(el("p", "No permitted evidence supplied.", "muted"));
        if (criterion.evidence_truncated)
          item.append(
            el("p", "Evidence references are limited in this view.", "muted"),
          );
        card.append(item);
      }
    } else
      card.append(
        this.open("View task", `task-${task.id}`, {
          tab: "tasks",
          kind: "task",
          id: task.id,
        }),
      );
    if (task.text_truncated)
      card.append(
        el("p", "Text is shortened to the viewer's detail limit.", "muted"),
      );
    return card;
  }
  private memory(memory: ViewerMemory, detail: boolean): HTMLElement {
    const card = el("article", "", "panel");
    card.append(
      pill(memory.authority),
      pill(memory.classification, "muted"),
      this.paragraph(memory.text),
      el("p", `Source: ${memory.source}`, "muted"),
      el(
        "p",
        `Observed ${new Date(memory.observed_ms).toISOString()}`,
        "eyebrow",
      ),
    );
    if (memory.truncated)
      card.append(
        el("p", "Text is shortened to the viewer's detail limit.", "muted"),
      );
    if (!detail)
      card.append(
        this.open("View memory", `memory-${memory.handle}`, {
          tab: "memories",
          kind: "memory",
          id: memory.handle,
        }),
      );
    return card;
  }
  private channel(report: ViewerReportItem | ViewerReportData): HTMLElement {
    const box = el("div", "", "report-status");
    box.append(
      pill(
        `Technical: ${report.technical_status}`,
        report.technical_status === "complete" ? "passed" : "pending",
      ),
      pill(`Review: ${report.review_status.replaceAll("_", " ")}`, "muted"),
      el(
        "p",
        `Revision ${report.revision} · ${report.program} · ${report.phase}`,
      ),
      el("p", "App: available"),
    );
    if (report.channel === "both") {
      const descriptions: Record<string, string> = {
        smtp_accepted: "Gmail accepted submission",
        queued: "Queued",
        submitting: "Submission in progress",
        rejected: "Submission rejected",
        unknown: "Submission uncertain · reconciliation needed",
        cancelled: "Cancelled",
      };
      box.append(
        el(
          "p",
          `Kindle submission: ${report.delivery_states.length ? report.delivery_states.map((state) => descriptions[state]).join("; ") : "No submission recorded"}`,
        ),
        el("p", "Kindle device arrival: unverified", "muted"),
      );
    } else
      box.append(
        el("p", "Kindle channel: not configured for this report", "muted"),
      );
    return box;
  }
  private reader(
    main: HTMLElement,
    metadata: ViewerReportData,
    state: State,
  ): void {
    const report = state.report;
    main.append(el("h2", metadata.title), this.channel(metadata));
    if (!report) return;
    const contents = el("details", "", "contents");
    contents.open = this.contentsOpen;
    contents.append(
      el(
        "summary",
        report.complete ? "Contents" : "Contents · loading remaining sections",
      ),
    );
    const article = el("article", "", "report-body");
    report.blocks.forEach((block, index) => {
      const node = this.reportBlock(block, state);
      article.append(node);
      if (block.kind === "heading")
        contents.append(
          this.action(block.text, `contents-${index}`, () => {
            node.scrollIntoView({ block: "start" });
            node.tabIndex = -1;
            node.focus({ preventScroll: true });
          }),
        );
    });
    main.append(
      el(
        "p",
        report.complete
          ? "Full document loaded."
          : `Loading document: ${report.page} of ${report.pages} fragments. Contents are incomplete until loading finishes.`,
        "reader-progress",
      ),
      contents,
      article,
    );
  }
  private reportBlock(block: Block, state: State): HTMLElement {
    if (block.kind === "heading")
      return el(
        `h${Math.max(2, Math.min(6, block.level + 1))}` as "h2",
        block.text,
      );
    if (block.kind === "code") {
      const pre = el("pre");
      pre.tabIndex = 0;
      pre.setAttribute("aria-label", "Code excerpt");
      pre.append(el("code", block.text));
      return pre;
    }
    if (block.kind === "table") {
      const wrapper = el("div", "", "table-scroll");
      wrapper.tabIndex = 0;
      wrapper.setAttribute("role", "region");
      wrapper.setAttribute("aria-label", "Report table");
      const table = el("table"),
        head = el("thead"),
        row = el("tr"),
        body = el("tbody");
      for (const text of block.headers) {
        const cell = el("th", text);
        cell.scope = "col";
        row.append(cell);
      }
      head.append(row);
      for (const cells of block.rows) {
        const row = el("tr");
        for (const text of cells) row.append(el("td", text));
        body.append(row);
      }
      table.append(head, body);
      wrapper.append(table);
      return wrapper;
    }
    if (block.kind === "figure") {
      const figure = el("figure"),
        caption = el("figcaption", block.caption);
      const url = state.figures.get(block.reference);
      if (url) {
        const image = el("img");
        image.src = url;
        image.alt = block.caption;
        figure.append(image);
      } else
        figure.append(
          this.action("Load figure", `figure-${block.reference}`, () => {
            void this.client.figure(block.reference);
          }),
        );
      figure.append(caption);
      return figure;
    }
    if (block.kind === "item") {
      const list = el("ul");
      const item = el("li");
      item.append(this.paragraph(block.text));
      list.append(item);
      return list;
    }
    return this.paragraph(block.text);
  }
  private settings(main: HTMLElement, state: State): void {
    const account = el("section", "", "panel");
    account.append(
      el("h2", "Session"),
      el("p", `Viewer: ${state.session?.binding.principal}`),
      el(
        "p",
        "GitHub identifies your account. MemoryKing grants determine what you can read.",
      ),
      this.action("Sign out", "logout", () => {
        void this.client.logout();
      }),
      this.action("Sign out all sessions", "logout-all", () => {
        void this.client.logout(true);
      }),
    );
    const display = el("section", "", "panel");
    display.append(el("h2", "Appearance"));
    const label = el("label", "Color theme");
    label.htmlFor = "theme";
    const theme = el("select");
    theme.id = "theme";
    theme.dataset.focus = "theme";
    for (const name of ["system", "dark", "light"]) {
      const option = el("option", name[0].toUpperCase() + name.slice(1));
      option.value = name;
      option.selected =
        name === (document.documentElement.dataset.theme ?? "system");
      theme.append(option);
    }
    theme.addEventListener("change", () => {
      if (!this.client.check()) return;
      document.documentElement.dataset.theme = theme.value;
      try {
        localStorage.setItem("memoryking-viewer-theme", theme.value);
      } catch {
        /* Appearance is optional. */
      }
    });
    display.append(
      label,
      theme,
      el(
        "p",
        "Only this appearance preference is saved on the device. Private content is not available offline.",
        "muted",
      ),
    );
    const install = el("section", "", "panel");
    install.append(
      el("h2", "Phone installation"),
      el(
        "p",
        "In Android Chrome, use the browser menu to add this dashboard to your home screen. Availability depends on your browser and trusted HTTPS setup.",
      ),
      el(
        "p",
        "Keep the approved private network connected. This app requires a running MemoryKing host and an internet connection for GitHub sign-in.",
      ),
      el(
        "p",
        "Read-only viewer. Agent work and permission changes use the separate supported interfaces.",
        "muted",
      ),
    );
    main.append(account, display, install);
  }
}
