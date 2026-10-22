import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { ChildThreadList, ChildThreadSummary, TurnDetail } from "../../../types/backend";
import type { useChildThreadConversation } from "../../../hooks/sessionTurnHistory/useChildThreadConversation";
import type { ChildThreadForkPoint } from "./ChildThreadConversation";
import {
  childThreadStatusClass,
  childThreadStatusLabel,
} from "../../../state/childThreadDisplay";
import ChildThreadPanel from "./ChildThreadPanel";

test("跨 Gateway 的子线程面板显示目标工作区和会话归属", () => {
  const html = renderToStaticMarkup(<ChildThreadPanel {...panelProps()} ownerLabel="gateway_remote · 目标工作区 · 目标会话" />);
  expect(html).toContain("gateway_remote · 目标工作区 · 目标会话");
  expect(html).toContain('data-child-owner-session="ses_parent"');
});

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
    thread_id: `thr_child_${index}`,
    delegation_id: `del_child_${index}`,
    title: `委派：子任务 ${index}`,
    created_at: "2026-09-15T12:00:00Z",
    collaboration_state: "published",
    admission_state: "bound",
    status: "running",
    subagent_type: "general-purpose",
    ...overrides,
  };
}

function panelProps(overrides: {
  onRefresh?: () => void;
  onSelectThread?: (threadId: string) => void;
} = {}) {
  return {
    threads: [] as ChildThreadSummary[],
    teams: [] as ChildThreadList["teams"],
    total: 0,
    loading: false,
    error: null as string | null,
    loadedAt: "2026-09-15T12:30:00Z",
    sessionId: "ses_parent",
    mainThreadId: "thr_main_canonical",
    ownerLabel: "本机 Gateway · 测试工作区 · 测试会话",
    activeThreadId: "thr_main_canonical",
    onRefresh: overrides.onRefresh ?? (() => {}),
    onSelectThread: overrides.onSelectThread ?? (() => {}),
    conversation: {
      selectedThread: null,
      selectionIntent: null,
      snapshot: { history: null, report: null, view: "history" as const, draft: "", loading: false, sending: false, error: null, accepted: null, paged: false },
      selectThread: () => {}, selectThreadForOwner: () => {}, close: () => {}, setDraft: () => {},
      refresh: async () => {}, openReport: async () => {}, loadEarlier: async () => {}, send: async () => {}, isUnread: () => false,
    },
  };
}

describe("子会话线程面板", () => {
  test("列表已确认缺少所选报告来源时明确提示，不回退主线程", () => {
    const props = panelProps();
    const html = renderToStaticMarkup(<ChildThreadPanel {...props} conversation={{ ...props.conversation,
      selectionIntent: { workspaceId: "workspace", sessionId: "ses_parent", threadId: "missing-child", view: "report", reportExecutionId: "old-execution" },
    }} />);
    expect(html).toContain("所选子线程已不可用");
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("child-thread-conversation");
  });

  test("子线程对话单次展开按原序展示压缩前后正文", async () => {
    const selected = thread(1);
    const turn: TurnDetail = {
      turn_id: "turn_child_order", job_id: "job_child_order", session_id: "ses_parent",
      source_view_id: "view_child_order", source_view_revision: 3,
      ordinal: 1, root_logical_item_ordinal: 0, revision: 1, status: "completed", items_view: "full",
      created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:01Z",
      user_messages: [{ message_id: "child_user_message", content: "子线程输入", attachments: [], created_at: "2026-09-15T12:00:00Z" }],
      final_response: "最终响应", items: [],
      activity_stats: { duration_ms: 100, item_count: 4 },
      response_parts: [
        { part_id: "child_before", kind: "reasoning", projection: "detail", status: "completed",
          source: { message_sequence: 1, item_id: "child_item_before", logical_item_ordinal: 1 }, text: "压缩前思考" },
        { part_id: "child_summary", kind: "compaction_summary", projection: "detail", status: "completed",
          source: { message_sequence: 2, item_id: "child_item_summary", logical_item_ordinal: 2 }, text: "压缩摘要" },
        { part_id: "child_after", kind: "reasoning", projection: "detail", status: "completed",
          source: { message_sequence: 3, item_id: "child_item_after", logical_item_ordinal: 3 }, text: "压缩后思考" },
        { part_id: "child_final", kind: "final_text", projection: "detail", status: "completed", final: true,
          source: { message_sequence: 4, item_id: "child_item_final", logical_item_ordinal: 4 }, text: "最终响应" },
      ],
    };
    const props = panelProps();
    const conversation: ReturnType<typeof useChildThreadConversation> = {
      ...props.conversation,
      selectedThread: selected,
      snapshot: { ...props.conversation.snapshot, history: {
        thread_id: selected.thread_id, source_view_id: "view_child_order", source_view_revision: 3,
        projection_epoch: 1, items: [turn], internal_executions: [],
      } },
    };
    const clicks: { threadId: string; point: ChildThreadForkPoint }[] = [];
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<ChildThreadPanel {...props} threads={[selected]} conversation={conversation}
        onForkFromThread={(threadId, point) => { clicks.push({ threadId, point }); }} />);
    });
    const sources = () => renderer!.root.findAll((node) =>
      typeof node.type === "string" && typeof node.props["data-response-sources"] === "string",
    ).flatMap((node) => JSON.parse(node.props["data-response-sources"]));
    const expected = turn.response_parts!.map(({ part_id, kind, source }) => ({ part_id, kind, source }));
    expect(sources()).toEqual([expected[1], expected[3]]);
    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    await act(async () => { await toggle.props.onClick(); });
    expect(sources()).toEqual(expected);
    expect(renderer!.root.findAllByProps({ className: "chat-thinking-toggle" })).toHaveLength(1);
    act(() => { renderer!.root.findByProps({ "data-fork-item-id": "child_item_after" }).props.onClick(); });
    expect(clicks).toEqual([{ threadId: selected.thread_id, point: {
      sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, itemId: "child_item_after",
    } }]);
    await act(async () => { await toggle.props.onClick(); });
    expect(sources()).toEqual([expected[1], expected[3]]);
    const userFork = renderer!.root.find((node) =>
      typeof node.props.className === "string"
        && node.props.className.split(" ").includes("child-thread-user"),
    ).findByType("button");
    act(() => { userFork.props.onClick(); });
    expect(clicks[clicks.length - 1]).toEqual({ threadId: selected.thread_id, point: {
      sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, turnId: turn.turn_id,
    } });
    const threadFork = renderer!.root.findAllByType("button").find((node) => node.props.children === "从此线程创建分支");
    expect(threadFork).toBeDefined();
    act(() => { threadFork!.props.onClick(); });
    expect(clicks[clicks.length - 1]).toEqual({ threadId: selected.thread_id, point: {} });
    act(() => renderer!.unmount());
  });

  test("团队投影展示同一 Session 的协调者、成员角色和任务状态", () => {
    const board: ChildThreadList["teams"][number] = {
      team_id: "team_owner", session_id: "ses_parent", name: "README 审查团队", coordinator_thread_id: "thr_main_canonical", version: 4,
      created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:30:00Z", recent_events: [],
      members: [
        { thread_id: "thr_main_canonical", role: "coordinator", source: "coordinator", work_mode: "write", instructions: "协调", status: "active", joined_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" },
        { thread_id: "thr_reviewer", role: "reviewer", source: "delegated", work_mode: "read_only", instructions: "审查", status: "active", joined_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" },
      ],
      tasks: [{ task_id: "ttask_review", title: "核对 README", description: "核对文件", phase: "review", cycle: 2, assignee_thread_id: "thr_reviewer", status: "blocked", depends_on_task_ids: [], summary: "等待正文确认", updated_by_thread_id: "thr_main_canonical", created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:30:00Z" }],
    };
    const html = renderToStaticMarkup(<ChildThreadPanel {...panelProps()} teams={[board]} />);
    for (const text of ["README 审查团队", "团队版本 4", "协调线程：thr_main_canonical", "coordinator", "reviewer", "核对 README", "受阻", "等待正文确认"]) expect(html).toContain(text);
    expect(html).toContain('data-team-version="4"');
    expect(html).toContain('data-team-task-status="blocked"');
    expect(html).not.toContain("当前会话还没有委托子会话");
  });

  test("列表渲染标题、subagent、时间与状态徽标", () => {
    const html = renderToStaticMarkup(
      <ChildThreadPanel
        {...panelProps()}
        threads={[
          thread(1),
          thread(2, {
            admission_state: "pending",
            status: "pending",
            title: "委派：等待中任务",
          }),
          thread(3, {
            collaboration_state: "cancelled",
            admission_state: "pending",
            status: "failed",
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
    expect(html).toContain("失败");
    // created_at 走 formatDateTime（本地时区），断言包含日期部分即可。
    expect(html).toContain("2026/09/15");
    expect(html).toContain('data-start-status="failed"');
    expect(html).toContain("child-thread-status-failed");
    expect(html).toContain("child-thread-status-pending");
    expect(html).toContain('data-thread-id="thr_main_canonical"');
    expect(html).toContain("主线程");
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

  test("main thread ID 尚未解析时不允许选择主线程", () => {
    const selected: string[] = [];
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({
            onSelectThread: (threadId) => { selected.push(threadId); },
          })}
          mainThreadId={null}
        />,
      );
    });

    const mainRow = renderer!.root.findByProps({
      "aria-label": "切换到主线程调试",
    });
    expect(mainRow.props.disabled).toBe(true);
    act(() => mainRow.props.onClick());
    expect(selected).toEqual([]);
    renderer!.unmount();
  });

  test("点击 child 项回传 thread_id，并支持切回主线程", () => {
    const selected: string[] = [];
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({
            onSelectThread: (threadId) => { selected.push(threadId); },
          })}
          threads={[thread(1), thread(2)]}
          activeThreadId="thr_child_2"
        />,
      );
    });

    const rows = renderer!.root.findAllByType("button").filter(
      (node) => String(node.props["aria-label"]).startsWith("调试子线程："),
    );
    expect(rows).toHaveLength(2);
    act(() => rows[0]!.props.onClick());
    const mainRow = renderer!.root.findByProps({
      "aria-label": "切换到主线程调试",
    });
    act(() => mainRow.props.onClick());
    expect(selected).toEqual(["thr_child_1", "thr_main_canonical"]);
    // 执行状态独立展示；调试 owner 通过专用按钮表达。
    const statuses = renderer!.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("child-thread-status"),
    );
    expect(statuses.map((node) => textOf(node))).toEqual(["主线程", "运行中", "运行中"]);
    expect(rows[1]!.props.title).toBe("当前调试 owner");
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
        "aria-label": "复制 child thread ID: thr_child_1",
      });
      await act(async () => {
        copyButton.props.onClick();
        // 复制链路经过若干微任务，等待一个宏任务确保状态落地。
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(written).toEqual(["thr_child_1"]);
      const notice = renderer!.root.findByProps({
        className: "child-thread-notice",
      });
      expect(textOf(notice)).toContain("已复制 child thread ID");
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
  test("协议状态映射中文标签和配色", () => {
    expect(childThreadStatusLabel("pending")).toBe("等待启动");
    expect(childThreadStatusLabel("running")).toBe("运行中");
    expect(childThreadStatusLabel("completed")).toBe("已完成");
    expect(childThreadStatusLabel("failed")).toBe("失败");
    expect(childThreadStatusClass("pending")).toBe("child-thread-status-pending");
    expect(childThreadStatusClass("running")).toBe("child-thread-status-running");
    expect(childThreadStatusClass("failed")).toBe("child-thread-status-failed");
  });
});
