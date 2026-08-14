import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import SessionActivityIndicator from "./SessionActivityIndicator";

describe("SessionActivityIndicator", () => {
  test("运行中优先显示旋转状态", () => {
    const html = renderToStaticMarkup(
      <SessionActivityIndicator running unread />,
    );

    expect(html).toContain('aria-label="会话正在运行"');
    expect(html).toContain("codicon-modifier-spin");
    expect(html).not.toContain("会话有未读结果");
  });

  test("任务结束后显示未读蓝标", () => {
    const html = renderToStaticMarkup(
      <SessionActivityIndicator running={false} unread />,
    );

    expect(html).toContain('aria-label="会话有未读结果"');
    expect(html).toContain("session-activity-indicator unread");
    expect(html).not.toContain("codicon-modifier-spin");
  });

  test("空闲且已读时不占用右侧空间", () => {
    expect(
      renderToStaticMarkup(
        <SessionActivityIndicator running={false} unread={false} />,
      ),
    ).toBe("");
  });
});
