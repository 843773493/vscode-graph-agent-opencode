import { describe, expect, test } from "bun:test";

function surfaceOpacity(css: string, token: string): number {
  const escapedToken = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const definition = css.match(
    new RegExp(
      `${escapedToken}\\s*:\\s*color-mix\\([\\s\\S]*?([0-9]+)%\\s*,\\s*transparent\\s*\\)`,
    ),
  );
  if (!definition) throw new Error(`找不到可计算的语义表面 token：${token}`);
  return Number(definition[1]) / 100;
}

describe("顶层语义表面", () => {
  test("顶层布局只使用语义表面 token", async () => {
    const css = await Bun.file(new URL("./themeSurfaces.css", import.meta.url)).text();

    for (const token of [
      "--bt-canvas-background",
      "--bt-chrome-surface",
      "--bt-toolbar-background",
      "--bt-workspace-surface",
      "--bt-panel-surface",
      "--bt-floating-surface",
      "--bt-critical-surface",
      "--bt-surface-backdrop-filter",
      "--bt-chrome-backdrop-filter",
      "--bt-workspace-backdrop-filter",
    ]) {
      expect(css).toContain(`var(${token})`);
    }
    expect(css).not.toContain("--bt-page-background");
    expect(css).not.toContain("--bt-panel-background");
    expect(css).not.toContain("--vscode-");
  });

  test("布局宿主透明且大面积工作区不叠加内容面板", async () => {
    const [css, app] = await Promise.all([
      Bun.file(new URL("./themeSurfaces.css", import.meta.url)).text(),
      Bun.file(new URL("../App.tsx", import.meta.url)).text(),
    ]);

    expect(css).toMatch(/\[data-bt-surface="layout"\][\s\S]*?background:\s*transparent/);
    expect(app).toMatch(/sessions-workbench-grid[\s\S]*?data-bt-surface="layout"/);
    expect(app).toMatch(/sessions-part-card[\s\S]*?data-bt-surface="workspace"/);
    expect(css).toMatch(
      /\[data-bt-surface="workspace"\][\s\S]*?backdrop-filter:\s*var\(--bt-workspace-backdrop-filter\)/,
    );
  });

  test("辅助区所有标签页及整区列表后代保持透明", async () => {
    const [surfaceCss, layoutCss, auxiliaryPanel] = await Promise.all([
      Bun.file(new URL("./themeSurfaces.css", import.meta.url)).text(),
      Bun.file(new URL("./workbenchLayout.css", import.meta.url)).text(),
      Bun.file(
        new URL(
          "../components/workspace/WorkspaceAuxiliaryPanel.tsx",
          import.meta.url,
        ),
      ).text(),
    ]);
    const contractStart = surfaceCss.indexOf(
      ".auxiliary-panel > .auxiliary-view-body",
    );
    const contractEnd = surfaceCss.indexOf("}", contractStart);
    const contract = surfaceCss.slice(contractStart, contractEnd + 1);

    expect(contractStart).toBeGreaterThanOrEqual(0);
    for (const selector of [
      ".auxiliary-panel > .auxiliary-view-body",
      ".auxiliary-resources-body > .resource-panel",
      ".auxiliary-resources-body .panel-list",
      ".auxiliary-resources-body .resource-tree",
      ".auxiliary-resources-body .empty-state",
    ]) {
      expect(contract).toContain(selector);
    }
    expect(contract).toMatch(/background:\s*transparent/);
    expect(auxiliaryPanel.match(/data-bt-surface="layout"/g)).toHaveLength(3);
    expect(layoutCss).not.toMatch(
      /\.auxiliary-resources-body\s*\{[^}]*background:/,
    );
    expect(layoutCss).not.toMatch(
      /\.auxiliary-resources-body\s+\.(?:resource-panel|panel-list|empty-state)\s*\{[^}]*background:/,
    );
  });

  test("默认遮罩下导航和工作区仍保留可感知的背景透出量", async () => {
    const themeCss = await Bun.file(new URL("./theme.css", import.meta.url)).text();
    const overlayOpacity = 0.44;
    const chromeVisibility =
      (1 - overlayOpacity) * (1 - surfaceOpacity(themeCss, "--bt-chrome-surface"));
    const workspaceVisibility =
      (1 - overlayOpacity) * (1 - surfaceOpacity(themeCss, "--bt-workspace-surface"));
    const panelOpacity = surfaceOpacity(themeCss, "--bt-panel-surface");

    expect(chromeVisibility).toBeGreaterThanOrEqual(0.12);
    expect(workspaceVisibility).toBeGreaterThanOrEqual(0.18);
    expect(panelOpacity).toBeGreaterThanOrEqual(0.8);
    expect(themeCss).toMatch(/--bt-workspace-backdrop-filter:\s*none/);
  });

  test("Gateway 下发的语义表面默认值与前端静态默认值一致", async () => {
    const [themeCss, builtins] = await Promise.all([
      Bun.file(new URL("./theme.css", import.meta.url)).text(),
      Bun.file(
        new URL("../../../../app/gateway/theme/builtins.py", import.meta.url),
      ).text(),
    ]);
    const normalize = (value: string | undefined) =>
      value
        ?.replace(/\s+/g, " ")
        .replace(/\(\s+/g, "(")
        .replace(/\s+\)/g, ")")
        .trim();

    for (const token of [
      "--bt-chrome-surface",
      "--bt-toolbar-background",
      "--bt-workspace-header-background",
      "--bt-workspace-surface",
      "--bt-panel-surface",
      "--bt-chrome-backdrop-filter",
      "--bt-workspace-backdrop-filter",
      "--bt-bottom-panel-background",
      "--bt-bottom-panel-header-background",
      "--bt-bottom-panel-toolbar-background",
      "--bt-bottom-panel-list-background",
      "--bt-bottom-panel-viewer-background",
      "--bt-runtime-preview-background",
      "--bt-runtime-preview-header-background",
      "--bt-runtime-preview-border",
      "--bt-status-bar-background",
      "--bt-status-bar-border",
      "--bt-status-bar-foreground",
    ]) {
      const cssValue = themeCss.match(
        new RegExp(`${token}\\s*:\\s*([^;]+);`),
      )?.[1];
      const pythonValue = builtins.match(
        new RegExp(`"${token}":\\s*"([^"]+)"`),
      )?.[1];
      expect(cssValue, `前端缺少 ${token}`).toBeTruthy();
      expect(pythonValue, `Gateway 缺少 ${token}`).toBeTruthy();
      expect(normalize(cssValue)).toBe(normalize(pythonValue));
    }
  });
});
