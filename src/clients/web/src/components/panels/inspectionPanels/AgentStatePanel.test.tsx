import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import AgentStatePanel from "./AgentStatePanel";

function renderPanel(jsonl: string): string {
  return renderToStaticMarkup(
    <AgentStatePanel
      jsonl={jsonl}
      messageCount={3}
      loadedAt={null}
      loading={false}
      error={null}
      port={8011}
      workspaceId="workspace_test"
      session={null}
      active={false}
    />,
  );
}

describe("AgentStatePanel 非法 JSONL 兜底", () => {
  test("刷新或失败不会替换已有状态正文，真实错误继续可见", () => {
    const props = { jsonl: JSON.stringify({ role: "user", content: "保留状态快照" }), messageCount: 1,
      loadedAt: null, port: 8011, workspaceId: "workspace_test", session: null, active: false };
    const loading = renderToStaticMarkup(<AgentStatePanel {...props} loading error={null} />);
    expect(loading).toContain("保留状态快照");
    expect(loading).toContain('aria-busy="true"');
    const failed = renderToStaticMarkup(<AgentStatePanel {...props} loading={false} error="HTTP 503：状态读取失败" />);
    expect(failed).toContain("保留状态快照");
    expect(failed).toContain("HTTP 503：状态读取失败");
    expect(failed).toContain('role="alert"');
  });

  test("含非法/截断/超长行时面板仍完整渲染并标注失败行", () => {
    const jsonl = [
      JSON.stringify({ role: "user", content: "正常消息" }),
      '{"role": "assistant", "content": ',
      "无法解析的一行",
      `{"role":"user","content":"${"y".repeat(1200)}` ,
    ].join("\n");

    // 关键断言：renderToStaticMarkup 会真实执行 render 阶段，裸 JSON.parse 会在此抛异常。
    const html = renderPanel(jsonl);

    expect(html).toContain("3 行 Agent State JSONL 无法解析");
    expect(html).toContain("第 2 行解析失败");
    expect(html).toContain("第 3 行解析失败");
    expect(html).toContain("第 4 行解析失败");
    // 合法行仍出现在原始快照中，说明白屏没有发生。
    expect(html).toContain("正常消息");
  });

  test("全部行合法时不出现失败提示", () => {
    const html = renderPanel(JSON.stringify({ role: "user", content: "只有正常行" }));

    expect(html).not.toContain("无法解析");
    expect(html).toContain("只有正常行");
  });
});
