import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import WorkbenchStatusBar from "./WorkbenchStatusBar";

describe("主窗口状态栏可见出口", () => {
  test("渲染 AppState 的进行中文案，失败文案同样可见", () => {
    const running = renderToStaticMarkup(
      <WorkbenchStatusBar status="正在切换 Agent: reviewer" themeBackgroundWarning={null} />,
    );
    expect(running).toContain("正在切换 Agent: reviewer");

    // 关键断言：hook 失败分支写入的「xxx 失败: message」必须在用户可见区域出现。
    const failed = renderToStaticMarkup(
      <WorkbenchStatusBar status="Agent 切换失败: 请求失败 500 : 上游模型不可用" themeBackgroundWarning={null} />,
    );
    expect(failed).toContain("Agent 切换失败: 请求失败 500 : 上游模型不可用");
    expect(failed).toContain('aria-label="状态栏"');
  });

  test("暴露 role=status 的无障碍语义", () => {
    const html = renderToStaticMarkup(
      <WorkbenchStatusBar status="已中断: stopped" themeBackgroundWarning={null} />,
    );
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
  });

  test("空状态不渲染文本节点", () => {
    const html = renderToStaticMarkup(
      <WorkbenchStatusBar status="" themeBackgroundWarning={null} />,
    );
    expect(html).not.toContain("workbench-status-text");
  });

  test("主题背景图失败降级为可见警告，且不覆盖 status", () => {
    const html = renderToStaticMarkup(
      <WorkbenchStatusBar
        status="工作区已就绪"
        themeBackgroundWarning="主题背景图加载失败，已回退为无背景图：背景图片加载失败: /api/gateway/ui-assets/bg"
      />,
    );
    expect(html).toContain("工作区已就绪");
    expect(html).toContain("主题背景图加载失败");
    expect(html).toContain("workbench-status-warning");
  });

  test("没有背景图警告时不渲染警告节点", () => {
    const html = renderToStaticMarkup(
      <WorkbenchStatusBar status="工作区已就绪" themeBackgroundWarning={null} />,
    );
    expect(html).not.toContain("workbench-status-warning");
  });
});
