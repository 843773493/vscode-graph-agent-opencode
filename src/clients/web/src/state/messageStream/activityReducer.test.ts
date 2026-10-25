import { describe, expect, test } from "bun:test";

import { modelOutputPhase } from "./activityReducer";

describe("modelOutputPhase", () => {
  test("把推理载体归入 reasoning 阶段", () => {
    for (const carrierType of [
      "reasoning",
      "reasoning_content",
      "reasoning_items",
      "thinking",
      "redacted_thinking",
    ]) {
      expect(modelOutputPhase(carrierType)).toBe("reasoning");
    }
  });

  test("把其他载体归入 text 阶段", () => {
    for (const carrierType of ["text", "image", "unknown", ""]) {
      expect(modelOutputPhase(carrierType)).toBe("text");
    }
  });
});
