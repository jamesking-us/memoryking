import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateExperience } from "../../packages/contracts/dist/index.js";

test("shared experience request vectors", () => {
  const vectors = JSON.parse(
    readFileSync("fixtures/contracts/experience-vectors.json", "utf8"),
  );
  for (const vector of vectors) {
    if (vector.valid)
      assert.doesNotThrow(
        () => validateExperience(vector.request),
        vector.name,
      );
    else assert.throws(() => validateExperience(vector.request), vector.name);
  }
});
