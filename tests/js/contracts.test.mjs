import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ContractError,
  canonicalBytes,
  loadStrict,
  validateContract,
} from "@memoryking/contracts";
import { phaseLabels } from "@memoryking/dashboard-foundation";

test("canonical JSON rejects unsafe values and unknown contracts", () => {
  for (const value of [
    NaN,
    Infinity,
    undefined,
    new Date(),
    2 ** 53,
    "\ud800",
  ]) {
    assert.throws(() => canonicalBytes(value), ContractError);
  }
  assert.throws(() => validateContract("Missing", {}), ContractError);
  assert.throws(
    () => loadStrict("[".repeat(66) + "0" + "]".repeat(66)),
    ContractError,
  );
});

test("Gmail acceptance does not imply Kindle receipt or approval", () => {
  const phase = JSON.parse(
    readFileSync("fixtures/contracts/phase.json", "utf8"),
  );
  phase.email_status = "smtp_accepted";
  phase.approval_status = "pending";
  assert.deepEqual(phaseLabels(phase), {
    technical: "in progress",
    delivery: "Gmail accepted; device unverified",
    review: "pending",
  });
});
