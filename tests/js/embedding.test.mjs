import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateEmbedding } from "../../packages/contracts/dist/index.js";

test("shared embedding request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/embedding-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(() => validateEmbedding(vector.request), vector.name);
    else assert.throws(() => validateEmbedding(vector.request), vector.name);
  }
});
