// Recall from a local daemon with the TypeScript client.
// Writing (remember, saveProgress, handOff) goes through MemoryKing, the shared-service client:
//
//   const memory = MemoryKing.connect(); // MEMORYKING_URL, _TENANT, _PROJECT, _CREDENTIAL
//   await memory.remember("Raising every timeout exceeded the total retry deadline.");
//   await memory.saveProgress("retry-fix", "Fix the retry deadline", "Investigating");
//
// Run through examples/client_typescript.py, which starts a real local daemon and passes
// its port, history id and credential file in MK_PORT, MK_INCARNATION and MK_CREDENTIAL.
import { MemoryKingError, MemoryKingLocal } from "@memoryking/client";

const memory = new MemoryKingLocal({
  port: Number(process.env.MK_PORT),
  project: "demo",
  incarnation: process.env.MK_INCARNATION,
  credentialFile: process.env.MK_CREDENTIAL,
});
try {
  const briefing = await memory.recall("timeout deadline");
  console.log(`${briefing.status}: ${briefing.sources.length} cited source`);
  console.log(briefing.text.split("\n")[0]);
  console.log("TypeScript client example passed.");
} catch (error) {
  if (error instanceof MemoryKingError) {
    console.error(error.code, error.hint);
    process.exitCode = 1;
  } else throw error;
}
