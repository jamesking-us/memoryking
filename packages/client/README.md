# @memoryking/client

A small TypeScript client for [MemoryKing](https://github.com/), the memory service for AI agents. It has no runtime dependencies, uses the `fetch` built into Node 22 and ships as an ES module with type declarations.

```ts
import { MemoryKing } from "@memoryking/client";

const memory = MemoryKing.connect(); // MEMORYKING_URL, _TENANT, _PROJECT, _CREDENTIAL

await memory.remember("Raising every timeout exceeded the total deadline.", {
  source: "pytest",
});
const briefing = await memory.recall("timeouts");
console.log(briefing.text); // cited Markdown; briefing.status is ready, partial or blocked

await memory.saveProgress(
  "retry-fix",
  "Fix the retry deadline",
  "Investigating",
  {
    nextActions: ["Run the regression"],
    shareWith: ["reviewer"],
  },
);
console.log((await memory.resume("retry-fix")).text);
await memory.handOff("retry-fix", "reviewer");
```

## What it talks to

- `MemoryKing` talks to a shared MemoryKing service over HTTPS (plain `http` only to a loopback address with `loopbackHttp: true`, for development). Calls go to the service's MCP endpoint, so the credential's grant, receipts and budgets are exactly those of the MCP tools. Redirects are refused and mutations are never retried automatically. To trust a private certificate authority, set Node's `NODE_EXTRA_CA_CERTS`.
- `MemoryKingLocal` recalls from a local daemon through its loopback agent endpoint. That endpoint serves retrieval only, so a local program that must write uses the Python client, the stdio MCP bridge, or the shared service.

## Credentials

Pass `credentialFile` (a private file, mode 0600, read again on every call so a rotation is picked up) or `credential`. `MemoryKing.connect()` reads `MEMORYKING_CREDENTIAL` as a file path. Use a credential scoped to what the agent needs; never log it.

## Errors

Failures throw `MemoryKingError` with the service's stable `code`, its one-line `hint` and the HTTP `status` when there was one. Transport failures use `transport_unavailable` and `request_timeout`.

```ts
try {
  await memory.handOff("retry-fix", "someone");
} catch (error) {
  if (error instanceof MemoryKingError) console.error(error.code, error.hint);
}
```

Every result carries the service's whole reply as `raw`.

## Idempotency

`remember` derives a stable event id from the source, project, day and text, so the same text from the same source on the same day replays instead of duplicating. Pass `event` to choose the id yourself. A save of progress is not replayed unless you retry it knowingly: check `revision` first.

Licensed under Apache-2.0.
