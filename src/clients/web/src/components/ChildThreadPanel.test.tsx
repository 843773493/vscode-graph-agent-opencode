import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { ChildThreadSummary } from "../types/backend";
import {
  childThreadStatusClass,
  childThreadStatusLabel,
} from "../state/childThreadDisplay";
import ChildThreadPanel from "./ChildThreadPanel";

/** 递归提取 react-test-renderer 节点的全部文本。 */
function textOf(node: { children: ReadonlyArray<unknown> }): string {
  return node.children
    .map((child) =>
      typeof child === "string"
        ? child
        : textOf(child as { children: ReadonlyArray<unknown> }),
    )
    .join("");
}

function thread(
  index: number,
  overrides: Partial<ChildThreadSummary> = {},
): ChildThreadSummary {
  return {
    session_id: `ses_child_${index}`,
    title: `委派：子任务 ${index}`,
    created_at: "2026-09-15T12:00:00Z",
    delegation_start_status: "running",
    start_error: null,
    parent_session_id: "ses_parent",
    subagent_type: "general-purpose",
    latest_job_status: null,
    ...overrides,
  };
}

function panelProps(overrides: {
  onRefresh?: () => void;
  onOpenSession?: (sessionId: string) => void;
} = {}) {
  return {
    threads: [] as ChildThreadSummary[],
    total: 0,
    loading: false,
    error: null as string | null,
    loadedAt: "2026-09-15T12:30:00Z",
    sessionId: "ses_parent",
    activeSessionId: "ses_parent" as string | null,
    onRefresh: overrides.onRefresh ?? (() => {}),
    onOpenSession: overrides.onOpenSession ?? (() => {}),
  };
}

describe("子会话线程面板", () => {
  test("列表渲染标题、subagent、时间与状态徽标", () => {
    const html = renderToStaticMarkup(
      <ChildThreadPanel
        {...panelProps()}
        threads={[
          thread(1),
          thread(2, {
            delegation_start_status: "pending",
            title: "委派：等待中任务",
          }),
          thread(3, {
            delegation_start_status: "failed",
            start_error: "subagent 启动超时",
            latest_job_status: null,
          }),
        ]}
        total={3}
      />,
    );

    expect(html).toContain("委派：子任务 1");
    expect(html).toContain("委派：等待中任务");
    expect(html).toContain("general-purpose");
    expect(html).toContain("等待启动");
    expect(html).toContain("运行中");
    expect(html).toContain("启动失败");
    // created_at 走 formatDateTime（本地时区），断言包含日期部分即可。
    expect(html).toContain("2026/09/15");
    expect(html).toContain('data-start-status="failed"');
    expect(html).toContain("child-thread-status-failed");
    expect(html).toContain("child-thread-status-pending");
    // 失败原因可展开查看。
    expect(html).toContain("启动失败详情");
    expect(html).toContain("subagent 启动超时");
    // 后端当前固定 null 的 latest_job_status 不渲染、不伪造。
    expect(html).not.toContain("最近任务状态");
  });

  test("空态与错误态互斥展示", () => {
    const emptyHtml = renderToStaticMarkup(<ChildThreadPanel {...panelProps()} />);
    expect(emptyHtml).toContain("当前会话还没有委托子会话");

    const errorHtml = renderToStaticMarkup(
      <ChildThreadPanel
        {...panelProps()}
        error="请求失败 409 Conflict: 会话目录索引异常"
      />,
    );
    expect(errorHtml).toContain("子会话线程加载失败");
    expect(errorHtml).toContain("会话目录索引异常");
    expect(errorHtml).not.toContain("当前会话还没有委托子会话");

    const noSessionHtml = renderToStaticMarkup(
      <ChildThreadPanel {...panelProps()} sessionId="" />,
    );
    expect(noSessionHtml).toContain("选择会话后查看其子会话线程");
  });

  test("刷新按钮触发 onRefresh，会话缺失时禁用", () => {
    let refreshes = 0;
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({ onRefresh: () => { refreshes += 1; } })}
          threads={[thread(1)]}
        />,
      );
    });

    const refreshButton = renderer!.root.findByProps({
      "aria-label": "刷新子会话线程",
    });
    act(() => refreshButton.props.onClick());
    expect(refreshes).toBe(1);
    expect(refreshButton.props.disabled).toBe(false);
    renderer!.unmount();

    let disabledRenderer: ReactTestRenderer;
    act(() => {
      disabledRenderer = create(
        <ChildThreadPanel {...panelProps()} sessionId="" />,
      );
    });
    const disabledButton = disabledRenderer!.root.findByProps({
      "aria-label": "刷新子会话线程",
    });
    expect(disabledButton.props.disabled).toBe(true);
    disabledRenderer!.unmount();
  });

  test("点击 child 项回传 session_id，当前会话行标记“当前”", () => {
    const opened: string[] = [];
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({
            onOpenSession: (sessionId) => { opened.push(sessionId); },
          })}
          threads={[thread(1), thread(2)]}
          activeSessionId="ses_child_2"
        />,
      );
    });

    const rows = renderer!.root.findAllByProps({ className: "child-thread-main" });
    expect(rows).toHaveLength(2);
    act(() => rows[0]!.props.onClick());
    expect(opened).toEqual(["ses_child_1"]);
    // 当前会话对应行的状态徽标显示“当前”。
    const statuses = renderer!.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("child-thread-status"),
    );
    expect(statuses.map((node) => textOf(node))).toEqual(["运行中", "当前"]);
    renderer!.unmount();
  });

  test("复制按钮把子会话 ID 写入剪贴板并提示", async () => {
    const written: string[] = [];
    // 测试环境没有真实剪贴板，mock 顶层 Clipboard API。
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (text: string) => {
          written.push(text);
        },
      },
      configurable: true,
    });

    try {
      let renderer: ReactTestRenderer;
      act(() => {
        renderer = create(
          <ChildThreadPanel {...panelProps()} threads={[thread(1)]} />,
        );
      });
      const copyButton = renderer!.root.findByProps({
        "aria-label": "复制子会话 ID: ses_child_1",
      });
      await act(async () => {
        copyButton.props.onClick();
        // 复制链路经过若干微任务，等待一个宏任务确保状态落地。
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(written).toEqual(["ses_child_1"]);
      const notice = renderer!.root.findByProps({
        className: "child-thread-notice",
      });
      expect(textOf(notice)).toContain("已复制子会话 ID");
      renderer!.unmount();
    } finally {
      Object.defineProperty(navigator, "clipboard", {
        value: originalClipboard,
        configurable: true,
      });
    }
  });
});

describe("子会话线程状态展示口径", () => {
  test("已知状态映射中文标签，未知状态透出原始值", () => {
    expect(childThreadStatusLabel("pending")).toBe("等待启动");
    expect(childThreadStatusLabel("running")).toBe("运行中");
    expect(childThreadStatusLabel("failed")).toBe("启动失败");
    expect(childThreadStatusLabel("future_status")).toBe("future_status");
    expect(childThreadStatusClass("pending")).toBe("child-thread-status-pending");
    expect(childThreadStatusClass("running")).toBe("child-thread-status-running");
    expect(childThreadStatusClass("failed")).toBe("child-thread-status-failed");
    expect(childThreadStatusClass("future_status")).toBe(
      "child-thread-status-unknown",
    );
  });
});
