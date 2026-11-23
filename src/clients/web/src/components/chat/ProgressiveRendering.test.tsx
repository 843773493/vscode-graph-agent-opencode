import { TranscriptTestView } from "../../tests/fixtures/transcriptView";
import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import MarkdownContent, {
  LARGE_MARKDOWN_DEFER_THRESHOLD,
} from "./MarkdownContent";
import { ProcessActivity } from "./turn/TurnActivitySummary";
import OriginalToolRow from "./ToolRow";
import ProgressiveUserMessage, {
  LARGE_USER_MESSAGE_RENDER_LIMIT,
} from "./turn/ProgressiveUserMessage";

describe("渐进 Markdown 渲染", () => {
  test("大型 Markdown 首次提交只渲染轻量文本", () => {
    const value = `# 标题\n\n${"包含表格和代码的正文 ".repeat(LARGE_MARKDOWN_DEFER_THRESHOLD)}`;
    const html = renderToStaticMarkup(<MarkdownContent value={value} />);

    expect(html).toContain('data-markdown-rendering="lightweight"');
    expect(html).toContain("# 标题");
    expect(html).not.toContain("<h1>");
    expect(html.length).toBeLessThan(value.length);

    const streaming = renderToStaticMarkup(<MarkdownContent value={`${value}最新尾部`} streaming />);
    expect(streaming).toContain("最新尾部");
    expect(streaming).not.toContain("# 标题");
    expect(streaming).toContain("展开全文");
    expect(streaming.length).toBeLessThan(value.length);
    const historical = renderToStaticMarkup(<MarkdownContent value={`${value}最新尾部`} preview="tail" />);
    expect(historical).toContain("最新尾部");
    expect(historical).not.toContain("# 标题");
  });

  test("大型 Markdown 在调度后原位增强", async () => {
    const value = `# 延迟标题\n\n${"正文 ".repeat(LARGE_MARKDOWN_DEFER_THRESHOLD)}`;
    let renderer: ReactTestRenderer | null = null;

    await act(async () => {
      renderer = create(<MarkdownContent value={value} />);
    });
    // 先提交组件并运行 effect，再等待它注册的增强任务，避免等待先于任务注册结束。
    await act(async () => {
      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 10));
    });

    expect(renderer!.root.findAll(
      (node) => node.props["data-markdown-rendering"] === "enhanced",
    )).toHaveLength(1);
    expect(renderer!.root.findAllByType("h1")).toHaveLength(1);
    expect(renderer!.root.findAllByType("button")[0].children.join(""))
      .toContain("展开全文");
    renderer!.unmount();
  });

  test("summary 的短文本也保持轻量模式，等待 full detail 替换", () => {
    const html = renderToStaticMarkup(
      <MarkdownContent value="**摘要正文**" renderMode="plain" />,
    );

    expect(html).toContain('data-markdown-rendering="lightweight"');
    expect(html).not.toContain("<strong>");
  });

  test("响应来源变化时刷新 Markdown 根节点的来源标记", async () => {
    let renderer: ReactTestRenderer | null = null;

    await act(async () => {
      renderer = create(
        <MarkdownContent value="同一段 Markdown" responseSources="来源 A" />,
      );
    });
    const markdownRoot = () => renderer!.root.findByProps({ className: "chat-markdown" });
    expect(markdownRoot().props["data-response-sources"]).toBe("来源 A");

    await act(async () => {
      renderer!.update(
        <MarkdownContent value="同一段 Markdown" responseSources="来源 B" />,
      );
    });

    expect(markdownRoot().props["data-response-sources"]).toBe("来源 B");
    renderer!.unmount();
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

  test("长用户输入截断不留下半个 emoji，展开后恢复完整原文", async () => {
    const content = `${"x".repeat(LARGE_USER_MESSAGE_RENDER_LIMIT - 1)}😀tail`;
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<ProgressiveUserMessage content={content} internalLabel={null} />); });
    try {
      const text = () => renderer.root.findByProps({ className: "chat-user-text" }).children.filter(value => typeof value === "string").join("");
      expect(text()).toBe(`${"x".repeat(LARGE_USER_MESSAGE_RENDER_LIMIT - 1)}\n\n…`);
      await act(async () => renderer.root.findByType("button").props.onClick());
      expect(text()).toBe(content);
    } finally { act(() => renderer.unmount()); }
  });
});

describe("折叠详情按需解析", () => {
  test("工具详情加载失败时展开并显示原因，而不是静默停在折叠态", async () => {
    let renderer: ReactTestRenderer | null = null;
    await act(async () => {
      renderer = create(
        <ToolRow
          showRawDetails={false}
          item={{
            kind: "aggregated_tool",
            id: "tool-load-failure",
            toolName: "custom_tool",
            toolCallId: "call-load-failure",
            inputText: "",
            resultText: "",
            timestamp: null,
            rawStart: {},
            rawEnd: {},
            active: false,
          }}
          onLoadDetails={async () => {
            throw new Error("详情读取超时");
          }}
        />,
      );
    });

    const summary = renderer!.root.findByProps({ className: "chat-tool-summary" });
    await act(async () => {
      await summary.props.onClick();
    });

    // 失败必须可见：展开且带 role=alert 的原因文案，不能只留下停止的转圈。
    expect(summary.props["aria-expanded"]).toBe(true);
    const alert = renderer!.root.findAll(
      (node) => node.props.role === "alert",
    );
    expect(alert).toHaveLength(1);
    expect(alert[0]!.children.join("")).toContain("详情读取超时");
    renderer!.unmount();
  });

  test("折叠 reasoning 不挂载完整 Markdown", () => {
    const hiddenTail = "REASONING_HIDDEN_TAIL";
    const html = renderToStaticMarkup(
      <TranscriptTestView><ProcessActivity
        process={{kind:"turn",id:"turn"}} settled stats={{duration_ms:7100,item_count:4,first_item_sequence:1,last_item_sequence:4}}
        showRawDetails={false}
        groups={[{kind:"work",id:"group",items:[{
          kind: "aggregated_text",
          id: "reasoning-1",
          text: `${"思考 ".repeat(80)}${hiddenTail}`,
          partKind: "reasoning",
          active: false,
          timestamp: null,
          eventCount: 1,
          rawEvents: [],
        }]}]}
      /></TranscriptTestView>,
    );

    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain("codicon-chevron-right");
    expect(html).toContain("7.1");
    expect(html).not.toContain("思考 思考");
    expect(html).not.toContain(hiddenTail);
  });

  test("展开 reasoning 使用向下箭头", () => {
    const html = renderToStaticMarkup(
      <TranscriptTestView><ProcessActivity
        process={{kind:"turn",id:"turn"}} settled={false}
        showRawDetails={false}
        groups={[{kind:"work",id:"group",items:[{
          kind: "aggregated_text",
          id: "reasoning-2",
          text: "正在思考",
          partKind: "reasoning",
          active: true,
          timestamp: null,
          eventCount: 1,
          rawEvents: [],
        }]}]}
      /></TranscriptTestView>,
    );

    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain("codicon-chevron-down");
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
    expect(html).toContain("codicon-chevron-right");
    expect(html).not.toContain(hiddenTail);
  });

  test("展开工具结果使用向下箭头", async () => {
    let renderer: ReactTestRenderer | null = null;

    await act(async () => {
      renderer = create(
        <ToolRow
          showRawDetails={false}
          item={{
            kind: "aggregated_tool",
            id: "tool-2",
            toolName: "custom_tool",
            inputText: "{}",
            resultText: "输出",
            timestamp: null,
            rawStart: {},
            rawEnd: {},
            active: false,
          }}
        />,
      );
    });

    const summary = renderer!.root.findByProps({ className: "chat-tool-summary" });
    expect(summary.props["aria-expanded"]).toBe(false);
    expect(renderer!.root.findByProps({ className: "codicon codicon-chevron-right" }))
      .toBeTruthy();

    await act(async () => {
      summary.props.onClick();
    });

    expect(summary.props["aria-expanded"]).toBe(true);
    expect(renderer!.root.findByProps({ className: "codicon codicon-chevron-down" }))
      .toBeTruthy();
    renderer!.unmount();
  });
});

function ToolRow(props: React.ComponentProps<typeof OriginalToolRow>) { return <TranscriptTestView><OriginalToolRow {...props} /></TranscriptTestView>; }
