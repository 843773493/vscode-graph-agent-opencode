import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import SessionChangesTree from "./SessionChangesTree";

/** 会话变更树的「加载失败」与「确实没有变更」必须是互斥的两个出口。 */
function tree(overrides: Partial<Parameters<typeof SessionChangesTree>[0]> = {}) {
  const props: Parameters<typeof SessionChangesTree>[0] = {
    changesets: [],
    selectedChangesetId: null,
    activeChangeset: null,
    loading: false,
    error: null,
    loadedAt: null,
    onSelectChangeset: () => {},
    onRefresh: () => {},
    onOpenFile: () => {},
    onReviewFile: async () => {},
  };
  return renderToStaticMarkup(<SessionChangesTree {...props} {...overrides} />);
}

describe("会话变更树失败态与空态口径", () => {
  test("读取失败时给出原因，且不得同时宣称「当前会话没有文件变更」", () => {
    // 可达路径：无会话时切到变更视图，后端尚未返回任何变更集就写入 error。
    const html = tree({ error: "当前没有会话可读取文件变更" });

    expect(html).toContain("当前没有会话可读取文件变更");
    expect(html).not.toContain("当前会话没有文件变更。");
    expect(html).not.toContain("没有可展示的会话文件变更。");
  });

  test("无错误且无变更时仍展示空态", () => {
    const html = tree();

    expect(html).toContain("当前会话没有文件变更。");
    expect(html).toContain("没有可展示的会话文件变更。");
  });

  test("读取中不展示错误或空态", () => {
    const html = tree({ loading: true });

    expect(html).toContain("正在读取会话变更...");
    expect(html).not.toContain("当前会话没有文件变更。");
    expect(html).not.toContain("没有可展示的会话文件变更。");
  });
});
