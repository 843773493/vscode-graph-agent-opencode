import { describe, expect, test } from "bun:test";
import { DEFAULT_THEME_BACKGROUND_OVERLAY, loadAndApplyBackground, preloadThemeBackground,
  DEFAULT_BACKGROUND, readAppearanceBackground, type AppearanceBackground } from "./theme";

function rootStub() {
  const values = new Map<string, string>();
  const style = {
    colorScheme: "dark",
    setProperty: (name: string, value: string) => { values.set(name, value); },
    removeProperty: (name: string) => { const previous = values.get(name) ?? ""; values.delete(name); return previous; },
  };
  return { root: { style } as unknown as HTMLElement, style, values };
}
const background = (url: string | null): AppearanceBackground => ({
  ...DEFAULT_BACKGROUND, image: url ?? "", position: "right center", size: "contain", repeat: "repeat-x",
});

describe("Workbench 宿主背景", () => {
  test("图片读取失败保留原始 URL", async () => {
    const image = {} as HTMLImageElement;
    const pending = preloadThemeBackground("/missing", () => image);
    image.onerror?.(new Event("error"));
    await expect(pending).rejects.toThrow("背景图片加载失败: /missing");
  });

  test("背景应用不覆写 Workbench 配色或明暗模式，并可清除旧布局参数", async () => {
    const { root, style, values } = rootStub();
    const image = {} as HTMLImageElement;
    const pending = loadAndApplyBackground(background("/image"), { root, createImage: () => image });
    image.onload?.(new Event("load"));
    expect((await pending).backgroundWarning).toBeNull();
    expect(Object.fromEntries(values)).toEqual({
      "--bt-background-image": 'url("/image")', "--bt-background-position": "right center",
      "--bt-background-size": "contain", "--bt-background-repeat": "repeat-x",
      "--bt-background-overlay": DEFAULT_THEME_BACKGROUND_OVERLAY,
    });
    expect(style.colorScheme).toBe("dark");
    await loadAndApplyBackground(background(null), { root });
    expect(Object.fromEntries(values)).toEqual({ "--bt-background-image": "none" });
  });

  test("失败只清除所属宿主背景，并返回可见警告", async () => {
    const first = rootStub(), second = rootStub();
    second.values.set("--bt-background-image", 'url("/second")');
    const image = {} as HTMLImageElement;
    const pending = loadAndApplyBackground(background("/missing"), { root: first.root, createImage: () => image });
    image.onerror?.(new Event("error"));
    expect((await pending).backgroundWarning).toContain("背景图片加载失败: /missing");
    expect(Object.fromEntries(first.values)).toEqual({ "--bt-background-image": "none" });
    expect(second.values.get("--bt-background-image")).toBe('url("/second")');
    expect(first.style.colorScheme).toBe("dark");
  });

  test.each(["load", "error"] as const)("取消后旧图片的 %s 回执不得改写当前背景", async outcome => {
    const { root, values } = rootStub();
    const image = {} as HTMLImageElement;
    const controller = new AbortController();
    const pending = loadAndApplyBackground(background("/old"), { root, createImage: () => image, signal: controller.signal });
    controller.abort(new Error("背景已切换"));
    await loadAndApplyBackground(background(null), { root });
    if (outcome === "load") image.onload?.(new Event("load"));
    else image.onerror?.(new Event("error"));
    await expect(pending).rejects.toThrow("背景已切换");
    expect(Object.fromEntries(values)).toEqual({ "--bt-background-image": "none" });
  });

  test("释放后的宿主不发起图片读取", async () => {
    const { root } = rootStub();
    const controller = new AbortController();
    controller.abort(new Error("宿主已释放"));
    await expect(loadAndApplyBackground(background("/closed"), { root, signal: controller.signal,
      createImage: () => { throw new Error("不应创建图片"); } },
    )).rejects.toThrow("宿主已释放");
  });
});


test("原生配置支持局部对象，并要求图片有明确 HTTP 目标", () => {
  expect(readAppearanceBackground({ image: "https://example.com/bg.png" })).toEqual({ ...DEFAULT_BACKGROUND, image: "https://example.com/bg.png" });
  for (const image of ["/api/gateway/ui-assets/id", "file:///tmp/bg.png"]) expect(() => readAppearanceBackground({ image })).toThrow();
  expect(() => readAppearanceBackground({ image: 42 })).toThrow();
  expect(() => readAppearanceBackground({ constructor: "unknown" })).toThrow();
});
