import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateWorking } from "../../packages/contracts/dist/index.js";

test("shared working-state request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/working-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(() => validateWorking(vector.request), vector.name);
    else assert.throws(() => validateWorking(vector.request), vector.name);
  }
});
