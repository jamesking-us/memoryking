import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const tokens = JSON.parse(
  readFileSync("packages/dashboard-foundation/tokens.json", "utf8"),
);
function luminance(hex) {
  const values = [1, 3, 5]
    .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
}
test("normal text pairs meet 4.5:1 in both themes", () => {
  for (const theme of [tokens.light, tokens.dark]) {
    for (const [a, b] of [
      [theme.text, theme.background],
      [theme.text, theme.surface],
      [theme.mutedText, theme.surface],
      [theme.accentText, theme.accent],
    ]) {
      const values = [luminance(a), luminance(b)].sort((a, b) => a - b);
      assert.ok((values[1] + 0.05) / (values[0] + 0.05) >= 4.5, `${a} on ${b}`);
    }
  }
});
