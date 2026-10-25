import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateHandoff } from "../../packages/contracts/dist/index.js";

test("shared recipient-bound handoff request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/handoff-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(() => validateHandoff(vector.request), vector.name);
    else assert.throws(() => validateHandoff(vector.request), vector.name);
  }
});
