import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateInference } from "../../packages/contracts/dist/index.js";

test("shared trusted dispatch inference request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/inference-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(() => validateInference(vector.request), vector.name);
    else assert.throws(() => validateInference(vector.request), vector.name);
  }
});
