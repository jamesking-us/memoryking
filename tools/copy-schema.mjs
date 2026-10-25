import { copyFileSync } from "node:fs";
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
