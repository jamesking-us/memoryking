import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateRepository } from "../../packages/contracts/dist/index.js";

test("shared repository request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/repository-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(
        () => validateRepository(vector.request),
        vector.name,
      );
    else assert.throws(() => validateRepository(vector.request), vector.name);
  }
});
