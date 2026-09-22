import { describe, expect, test } from "bun:test";
import {
  applyBoxTeamTheme,
  DEFAULT_THEME_BACKGROUND_OVERLAY,
  loadAndApplyResolvedGatewayTheme,
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

  test("无 document 环境显式传入 root 时不访问 document，也不抛错", () => {
    expect(typeof document).toBe("undefined");
    const { root, style } = createRootStub();

    applyBoxTeamTheme({
      id: "ssr-theme",
      tokens: { "--bt-page-background": "#101010" },
    }, root);

    expect(style.values.get("--bt-page-background")).toBe("#101010");
  });

  test("两个 root 交替应用主题时 token 集合互相独立", () => {
    const first = createRootStub();
    const second = createRootStub();

    applyBoxTeamTheme(
      { id: "a", tokens: { "--bt-page-background": "#a1a1a1", "--bt-text-primary": "#a2a2a2" } },
      first.root,
    );
    applyBoxTeamTheme(
      { id: "b", tokens: { "--bt-panel-background": "#b1b1b1" } },
      second.root,
    );

    // 第二个 root 的应用不得清掉第一个 root 的 token。
    expect(first.style.values.get("--bt-page-background")).toBe("#a1a1a1");
    expect(first.style.values.get("--bt-text-primary")).toBe("#a2a2a2");
    expect(second.style.values.get("--bt-panel-background")).toBe("#b1b1b1");
    expect(second.style.values.has("--bt-page-background")).toBe(false);

    // 各自重新应用时只清理自己那份。
    applyBoxTeamTheme({ id: "a2" }, first.root);
    expect(first.style.values.has("--bt-page-background")).toBe(false);
    expect(second.style.values.get("--bt-panel-background")).toBe("#b1b1b1");
  });

  test("背景图加载失败只降级为可见警告，核心主题仍成功应用", async () => {
    const { root, style } = createRootStub();
    const image = {} as HTMLImageElement;

    const pending = loadAndApplyResolvedGatewayTheme(
      {
        id: "warm",
        color_scheme: "light",
        tokens: { "--bt-page-background": "#f2ecd9" },
        background_image_url: "/api/gateway/ui-assets/missing",
      },
      { root, createImage: () => image },
    );
    image.onerror?.(new Event("error"));
    const result = await pending;

    // 关键断言：背景图失败不再 reject，调用方不会把它当成工作区初始化失败。
    expect(result.backgroundWarning).toContain(
      "背景图片加载失败: /api/gateway/ui-assets/missing",
    );
    expect(style.values.get("--bt-page-background")).toBe("#f2ecd9");
    expect(style.values.get("--bt-background-image")).toBe("none");
  });

  test("背景图加载成功时不产生警告并应用背景图", async () => {
    const { root, style } = createRootStub();
    const image = {} as HTMLImageElement;

    const pending = loadAndApplyResolvedGatewayTheme(
      {
        id: "warm",
        color_scheme: "light",
        tokens: { "--bt-page-background": "#f2ecd9" },
        background_image_url: "https://example.com/bg.png",
      },
      { root, createImage: () => image },
    );
    image.onload?.(new Event("load"));

    expect((await pending).backgroundWarning).toBeNull();
    expect(style.values.get("--bt-background-image")).toBe(
      'url("https://example.com/bg.png")',
    );
  });
});
