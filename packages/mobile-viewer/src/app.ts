import { ViewerClient } from "./client.js";
import { Presentation } from "./presentation.js";

const root = document.getElementById("app");
if (!root) throw new Error("missing_shell");
try {
  const theme = localStorage.getItem("memoryking-viewer-theme");
  if (theme && ["system", "dark", "light"].includes(theme))
    document.documentElement.dataset.theme = theme;
} catch {
  /* Appearance is optional. */
}
const presentation = new Presentation(root);
const client = new ViewerClient((state) => {
  try {
    presentation.render(state);
  } catch {
    client.clear("View unavailable. Reload to check current access.");
  }
});
presentation.client = client;
presentation.render(client.state);

// Capture-phase checks precede every private action, including a timer-suspended
// page's first input. Expiry also clears through a timer; neither is sufficient alone.
for (const type of [
  "pointerdown",
  "keydown",
  "click",
  "beforeinput",
  "focusin",
]) {
  document.addEventListener(
    type,
    (event) => {
      if (client.state.status === "ready" && !client.check()) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );
}
document.addEventListener(
  "scroll",
  () => {
    if (client.state.status === "ready") client.check();
  },
  { capture: true, passive: true },
);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) client.pause();
  else client.resume();
});
window.addEventListener("pagehide", () => client.pause());
window.addEventListener("pageshow", () => client.resume());
window.addEventListener("offline", () => client.offline());
window.addEventListener("online", () => client.resume());
window.addEventListener("popstate", () => client.resume());
// Avoid creating a second startup request when the initial pageshow is still due.
if (document.readyState === "complete") client.resume();
