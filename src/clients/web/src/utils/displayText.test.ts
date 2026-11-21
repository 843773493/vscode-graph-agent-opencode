import { expect, test } from "bun:test";
import { truncateDisplayText } from "./displayText";
import { compactKeyFlowText } from "../state/skillKeyFlow";

test("截断遵守 code unit 上限，保留完整的 emoji 和扩展汉字", () => {
  for (const value of ["😀", "𠀀"]) {
    expect(truncateDisplayText(`ab${value}cd`, 3)).toBe("ab");
    expect(truncateDisplayText(`ab${value}cd`, 4)).toBe(`ab${value}`);
    expect(truncateDisplayText(`ab${value}cd`, 9)).toBe(`ab${value}cd`);
  }
  expect(truncateDisplayText("中文abc", 2)).toBe("中文");
  expect(truncateDisplayText("😀", 0)).toBe("");
  expect(truncateDisplayText("", 20)).toBe("");
});

test("Skill 摘要在边界只删去放不下的完整码点", () => {
  expect(compactKeyFlowText(`${"a".repeat(119)}😀tail`)).toBe(`${"a".repeat(119)}...`);
  expect(compactKeyFlowText(`${"a".repeat(118)}😀tail`)).toBe(`${"a".repeat(118)}😀...`);
});
