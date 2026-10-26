import { copyFileSync, mkdirSync } from "node:fs";
mkdirSync("src/memoryking/adapters/viewer_web", { recursive: true });
for (const name of [
  "index.html",
  "shell.css",
  "manifest.webmanifest",
  "elephant.png",
  "icon-192.png",
  "icon-512.png",
]) {
  copyFileSync(
    `packages/mobile-viewer/web/${name}`,
    `src/memoryking/adapters/viewer_web/${name}`,
  );
}
for (const name of [
  "app",
  "client",
  "presentation",
  "freshness",
  "network",
  "report",
  "graph",
]) {
  copyFileSync(
    `packages/mobile-viewer/dist/${name}.js`,
    `src/memoryking/adapters/viewer_web/${name}.js`,
  );
}
for (const stem of ["viewer", "reporting"]) {
  copyFileSync(
    `packages/contracts/${stem}.schema.json`,
    `packages/contracts/dist/${stem}.schema.json`,
  );
}
copyFileSync(
  "packages/contracts/embedding.schema.json",
  "packages/contracts/dist/embedding.schema.json",
);
copyFileSync(
  "packages/contracts/schema.json",
  "packages/contracts/dist/schema.json",
);
copyFileSync(
  "packages/contracts/retrieval.schema.json",
  "packages/contracts/dist/retrieval.schema.json",
);

for (const name of [
  "index.html",
  "app.css",
  "elephant.png",
  "crown.svg",
  "favicon.svg",
  "wordmark.svg",
  "crown-mono.svg",
]) {
  copyFileSync(
    `packages/dashboard-foundation/web/${name}`,
    `src/memoryking/adapters/web/${name}`,
  );
}
for (const name of [
  "app",
  "observatory",
  "graph",
  "graph-layout",
  "graph-worker",
  "brand",
]) {
  copyFileSync(
    `packages/dashboard-foundation/dist/${name}.js`,
    `src/memoryking/adapters/web/${name}.js`,
  );
}

copyFileSync(
  "packages/contracts/working.schema.json",
  "packages/contracts/dist/working.schema.json",
);

copyFileSync(
  "packages/contracts/handoff.schema.json",
  "packages/contracts/dist/handoff.schema.json",
);

copyFileSync(
  "packages/contracts/inference.schema.json",
  "packages/contracts/dist/inference.schema.json",
);

copyFileSync(
  "packages/contracts/repository.schema.json",
  "packages/contracts/dist/repository.schema.json",
);

copyFileSync(
  "packages/contracts/processing.schema.json",
  "packages/contracts/dist/processing.schema.json",
);

copyFileSync(
  "packages/contracts/experience.schema.json",
  "packages/contracts/dist/experience.schema.json",
);
