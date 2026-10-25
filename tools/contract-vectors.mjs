import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import {
  loadStrict,
  canonicalBytes,
  canonicalHash,
  validateContract,
  ContractError,
} from "@memoryking/contracts";
const vectors = JSON.parse(
  readFileSync("fixtures/contracts/vectors.json", "utf8"),
);
const results = vectors.map((vector) => {
  try {
    const value = loadStrict(vector.raw);
    if (vector.contract) validateContract(vector.contract, value);
    const canonical = canonicalBytes(value).toString("utf8");
    assert.notEqual(vector.valid, false, vector.id);
    if (vector.canonical) assert.equal(canonical, vector.canonical, vector.id);
    return {
      id: vector.id,
      valid: true,
      canonical,
      hash: canonicalHash(value),
    };
  } catch (error) {
    if (!(error instanceof ContractError)) throw error;
    assert.equal(vector.valid, false, vector.id);
    return { id: vector.id, valid: false };
  }
});
console.log(JSON.stringify(results));
