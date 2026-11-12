import React from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import SessionActivityIndicator from "./SessionActivityIndicator";
import { setLocaleResources } from "../../../../shared/core/i18n/index.js";
import english from "../../../../shared/core/i18n/messages/en.json";
import chinese from "../../../../shared/core/i18n/messages/zh-cn.json";

afterEach(() => setLocaleResources("zh-cn", chinese));

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

  test("英文无障碍状态仍由运行态优先，并在任务结束后显示未读结果", () => {
    setLocaleResources("en", english);
    const running = renderToStaticMarkup(<SessionActivityIndicator running unread />);
    expect(running).toContain('aria-label="Session is running"');
    expect(running).not.toContain("Session has unread results");
    const unread = renderToStaticMarkup(<SessionActivityIndicator running={false} unread />);
    expect(unread).toContain('aria-label="Session has unread results"');
    expect(unread).not.toContain("codicon-modifier-spin");
  });
});
