import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateRetrieval } from "../../packages/contracts/dist/index.js";

test("shared retrieval request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/retrieval-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(() => validateRetrieval(vector.request), vector.name);
    else assert.throws(() => validateRetrieval(vector.request), vector.name);
  }
});
