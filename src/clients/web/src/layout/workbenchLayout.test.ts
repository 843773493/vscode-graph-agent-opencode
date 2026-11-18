import { describe, expect, test } from "bun:test";
import {
  DEFAULT_MAIN_AREA_RATIOS,
  resizeThreadResources,
  resolveMainAreaRatios,
} from "./workbenchLayout";

describe("会话资源布局", () => {
  test("未保存布局使用权威默认比例", () => {
    expect(resolveMainAreaRatios(null)).toEqual(DEFAULT_MAIN_AREA_RATIOS);
  });

  test("分隔线按实际区域尺寸分配比例", () => {
    expect(resizeThreadResources({ leftWidth: 400, rightWidth: 400, deltaX: 100 })).toBe(0.375);
  });

  test("窄组使用单区域导航，不允许无解的并排尺寸", () => {
    expect(() => resizeThreadResources({ leftWidth: 200, rightWidth: 200, deltaX: 1 })).toThrow('520px');
  });

  test.each([
    { deltaX: -2000, chat: 280, resources: 520 },
    { deltaX: 2000, chat: 560, resources: 240 },
  ])("越界拖动$deltaX像素停在可用边界", ({ deltaX, chat, resources }) => {
    const ratio = resizeThreadResources({ leftWidth: 400, rightWidth: 400, deltaX });
    expect(ratio * 800).toBeCloseTo(resources);
    expect((1 - ratio) * 800).toBeCloseTo(chat);
  });
});
