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
      sessionId="ses_agent_state"
      active={false}
    />,
  );
}

describe("AgentStatePanel 非法 JSONL 兜底", () => {
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
