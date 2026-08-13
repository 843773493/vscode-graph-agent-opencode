import { describe, expect, test } from "bun:test";
import {
  applyBoxTeamTheme,
  DEFAULT_THEME_BACKGROUND_OVERLAY,
  preloadThemeBackground,
  type BoxTeamThemeConfig,
} from "./theme";

class ThemeStyleStub {
  readonly values = new Map<string, string>();
  colorScheme = "";

  setProperty(name: string, value: string): void {
    this.values.set(name, value);
  }

  removeProperty(name: string): string {
    const previous = this.values.get(name) ?? "";
    this.values.delete(name);
    return previous;
  }
}

function createRootStub(): {
  root: HTMLElement;
  style: ThemeStyleStub;
  dataset: Record<string, string>;
} {
  const style = new ThemeStyleStub();
  const dataset: Record<string, string> = {};
  return {
    root: { style, dataset } as unknown as HTMLElement,
    style,
    dataset,
  };
}

describe("BoxTeam 运行时主题", () => {
  test("背景图片默认遮罩使用中性暗色而不是主题底色", () => {
    expect(DEFAULT_THEME_BACKGROUND_OVERLAY).toContain("rgb(17 19 24");
    expect(DEFAULT_THEME_BACKGROUND_OVERLAY).not.toContain("--bt-page-background");
    expect(DEFAULT_THEME_BACKGROUND_OVERLAY).not.toBe("none");
  });

  test("背景图片加载失败时返回包含 URL 的显式错误", async () => {
    const image = {} as HTMLImageElement;
    const pending = preloadThemeBackground(
      "/api/gateway/ui-assets/missing",
      () => image,
    );
    image.onerror?.(new Event("error"));
    await expect(pending).rejects.toThrow(
      "背景图片加载失败: /api/gateway/ui-assets/missing",
    );
  });

  test("统一应用主题 token、配色模式和背景图", () => {
    const { root, style, dataset } = createRootStub();

    applyBoxTeamTheme({
      id: "plugin-theme",
      colorScheme: "dark",
      tokens: {
        "--bt-page-background": "#201d18",
        "--bt-text-primary": "#f8f1df",
      },
      backgroundImage: "https://example.com/theme image.png",
    }, root);

    expect(dataset.boxteamTheme).toBe("plugin-theme");
    expect(style.colorScheme).toBe("dark");
    expect(style.values.get("--bt-page-background")).toBe("#201d18");
    expect(style.values.get("--bt-background-image")).toBe(
      'url("https://example.com/theme image.png")',
    );

    applyBoxTeamTheme({ id: "warm" }, root);
    expect(style.values.has("--bt-page-background")).toBe(false);
    expect(style.values.get("--bt-background-image")).toBe("none");
  });

  test("拒绝非 BoxTeam 命名空间的外部变量", () => {
    const { root, style } = createRootStub();
    applyBoxTeamTheme({
      tokens: { "--bt-page-background": "#f2ecd9" },
    }, root);
    const invalidConfig = {
      tokens: { "--foreign-background": "red" },
    } as unknown as BoxTeamThemeConfig;

    expect(() => applyBoxTeamTheme(invalidConfig, root)).toThrow(
      "主题变量必须使用 --bt- 前缀",
    );
    expect(style.values.get("--bt-page-background")).toBe("#f2ecd9");
  });
});
