import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import MarkdownContent, {
  LARGE_MARKDOWN_DEFER_THRESHOLD,
} from "./MarkdownContent";
import ThinkingSection from "./ThinkingSection";
import ToolRow from "./ToolRow";
import ProgressiveUserMessage, {
  LARGE_USER_MESSAGE_RENDER_LIMIT,
} from "./ProgressiveUserMessage";

describe("渐进 Markdown 渲染", () => {
  test("大型 Markdown 首次提交只渲染轻量文本", () => {
    const value = `# 标题\n\n${"包含表格和代码的正文 ".repeat(LARGE_MARKDOWN_DEFER_THRESHOLD)}`;
    const html = renderToStaticMarkup(<MarkdownContent value={value} />);

    expect(html).toContain('data-markdown-rendering="lightweight"');
    expect(html).toContain("# 标题");
    expect(html).not.toContain("<h1>");
  });

  test("大型 Markdown 在调度后原位增强", async () => {
    const value = `# 延迟标题\n\n${"正文 ".repeat(LARGE_MARKDOWN_DEFER_THRESHOLD)}`;
    let renderer: ReactTestRenderer | null = null;

    await act(async () => {
      renderer = create(<MarkdownContent value={value} />);
      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 10));
    });

    expect(renderer!.root.findAll(
      (node) => node.props["data-markdown-rendering"] === "enhanced",
    )).toHaveLength(1);
    expect(renderer!.root.findAllByType("h1")).toHaveLength(1);
    expect(renderer!.root.findAllByType("button")[0].children.join(""))
      .toContain("渲染完整 Markdown");
    renderer!.unmount();
  });

  test("summary 的短文本也保持轻量模式，等待 full detail 替换", () => {
    const html = renderToStaticMarkup(
      <MarkdownContent value="**摘要正文**" renderMode="plain" />,
    );

    expect(html).toContain('data-markdown-rendering="lightweight"');
    expect(html).not.toContain("<strong>");
  });

  test("超大用户输入只挂载有界预览", () => {
    const hiddenTail = "USER_MESSAGE_HIDDEN_TAIL";
    const content = `用户输入${"x".repeat(LARGE_USER_MESSAGE_RENDER_LIMIT)}${hiddenTail}`;
    const html = renderToStaticMarkup(
      <ProgressiveUserMessage content={content} internalLabel={null} />,
    );

    expect(html).toContain("显示完整输入");
    expect(html).not.toContain(hiddenTail);
  });
});

describe("折叠详情按需解析", () => {
  test("折叠 reasoning 不挂载完整 Markdown", () => {
    const hiddenTail = "REASONING_HIDDEN_TAIL";
    const html = renderToStaticMarkup(
      <ThinkingSection
        active={false}
        showRawDetails={false}
        items={[{
          kind: "aggregated_text",
          id: "reasoning-1",
          text: `${"思考 ".repeat(80)}${hiddenTail}`,
          partKind: "reasoning",
          active: false,
          timestamp: null,
          eventCount: 1,
          rawEvents: [],
        }]}
      />,
    );

    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain(hiddenTail);
  });

  test("折叠工具结果不挂载大型输出", () => {
    const hiddenTail = "TOOL_HIDDEN_TAIL";
    const html = renderToStaticMarkup(
      <ToolRow
        showRawDetails={false}
        item={{
          kind: "aggregated_tool",
          id: "tool-1",
          toolName: "custom_tool",
          inputText: "{}",
          resultText: `${"输出 ".repeat(80)}${hiddenTail}`,
          timestamp: null,
          rawStart: {},
          rawEnd: {},
          active: false,
        }}
      />,
    );

    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain(hiddenTail);
  });
});
