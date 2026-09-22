import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import WorkbenchStatusBar from "./WorkbenchStatusBar";

describe("主窗口状态栏可见出口", () => {
  test("渲染 AppState 的进行中文案，失败文案同样可见", () => {
    const running = renderToStaticMarkup(
      <WorkbenchStatusBar status="正在切换 Agent: reviewer" />,
    );
    expect(running).toContain("正在切换 Agent: reviewer");

    // 关键断言：hook 失败分支写入的「xxx 失败: message」必须在用户可见区域出现。
    const failed = renderToStaticMarkup(
      <WorkbenchStatusBar status="Agent 切换失败: 请求失败 500 : 上游模型不可用" />,
    );
    expect(failed).toContain("Agent 切换失败: 请求失败 500 : 上游模型不可用");
    expect(failed).toContain('aria-label="状态栏"');
  });

  test("暴露 role=status 的无障碍语义", () => {
    const html = renderToStaticMarkup(<WorkbenchStatusBar status="已中断: stopped" />);
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
  });

  test("空状态不渲染文本节点", () => {
    const html = renderToStaticMarkup(<WorkbenchStatusBar status="" />);
    expect(html).not.toContain("workbench-status-text");
  });
});
