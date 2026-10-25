import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateProcessing } from "../../packages/contracts/dist/index.js";

test("shared processing request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/processing-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(
        () => validateProcessing(vector.request),
        vector.name,
      );
    else assert.throws(() => validateProcessing(vector.request), vector.name);
  }
});
