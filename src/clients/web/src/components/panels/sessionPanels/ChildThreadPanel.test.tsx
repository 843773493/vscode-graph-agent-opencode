import { afterEach, describe, expect, test } from "bun:test";
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
import { ChatItemMenuContext, type ChatItemMenuRequest } from "../../chat/itemContextMenu";
import { VirtuosoMockContext } from "react-virtuoso";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

let animationFrameDescriptors: [PropertyDescriptor | undefined, PropertyDescriptor | undefined] | null = null;
const animationFrames = new Set<number>();
afterEach(() => {
  if (!animationFrameDescriptors) return;
  for (const id of animationFrames) clearTimeout(id);
  animationFrames.clear();
  restoreGlobalDescriptor("requestAnimationFrame", animationFrameDescriptors[0]);
  restoreGlobalDescriptor("cancelAnimationFrame", animationFrameDescriptors[1]);
  animationFrameDescriptors = null;
});

test("会话展开后直接展示 Thread，不插入标题、计数、归属说明或空提示栏", () => {
  const html = renderToStaticMarkup(<ChildThreadPanel {...panelProps()} />);
  expect(html).toContain('data-child-owner-session="ses_parent"');
  expect(html).toContain('data-thread-id="thr_main_canonical"');
  for (const name of ["panel-header", "resource-total-count", "child-thread-notice-slot"]) {
    expect(html).not.toContain(`class="${name}"`);
  }
  expect(html).not.toContain("刷新会话线程");
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
  onOpenMainThread?: () => void;
} = {}) {
  return {
    threads: [] as ChildThreadSummary[],
    teams: [] as ChildThreadList["teams"],
    loading: false,
    error: null as string | null,
    loadedAt: "2026-09-15T12:30:00Z",
    sessionId: "ses_parent",
    mainThreadId: "thr_main_canonical",
    activeThreadId: "thr_main_canonical",
    onRefresh: overrides.onRefresh ?? (() => {}),
    onOpenThread: (_thread: ChildThreadSummary) => {},
    onOpenMainThread: overrides.onOpenMainThread ?? (() => {}),
    conversation: {
      selectedThread: null,
      owner: null,
      selectionIntent: null,
      submissionRecovery: null,
      snapshot: { history: null, report: null, view: "history" as const, draft: "", loading: false, sending: false, error: null, accepted: null, paged: false },
      selectThread: () => {}, selectThreadForOwner: () => {}, close: () => {}, setDraft: () => {},
    loadProcessDetails: async () => {}, refresh: async () => {}, openReport: async () => {}, loadEarlier: async () => {}, send: async () => {}, isUnread: () => false,
      retainExecutionBodies: async () => {}, reportHistoryError: () => {},
    },
  };
}

describe("子会话线程面板", () => {
  test("没有报告且未展开的子线程也能按精确 owner 创建整线程分支", () => {
    const children = [thread(1), thread(2)];
    const forks: { threadId: string; point: ChildThreadForkPoint }[] = [];
    const opened: ChildThreadSummary[] = [];
    const selected: string[] = [];
    const props = panelProps();
    let renderer!: ReactTestRenderer;
    try {
      act(() => { renderer = create(<ChildThreadPanel {...props} threads={children}
        onOpenThread={value => opened.push(value)}
        onForkFromThread={(threadId, point) => forks.push({ threadId, point })}
        conversation={{ ...props.conversation, selectThread: id => selected.push(id) }} />); });
      expect(renderer.root.findAllByProps({ className: "child-thread-report-link" })).toHaveLength(0);
      const row = renderer.root.findByProps({ "data-thread-id": children[1].thread_id });
      act(() => row.findByProps({ "aria-label": "从此线程创建分支" }).props.onClick());
      expect(forks).toEqual([{ threadId: children[1].thread_id, point: {} }]);
      expect(opened).toEqual([]);
      expect(selected).toEqual([]);
    } finally { act(() => renderer?.unmount()); }
  });

  test("原生工作台通过回调打开完整 Thread 编辑器", () => {
    const child = thread(1);
    const opened: ChildThreadSummary[] = [];
    const embedded: string[] = [];
    const props = panelProps();
    let renderer!: ReactTestRenderer;
    try {
      act(() => { renderer = create(<ChildThreadPanel {...props} threads={[child]}
        onOpenThread={value => opened.push(value)} conversation={{ ...props.conversation,
          selectThread: id => embedded.push(id),
        }} />); });
      const button = renderer.root.findAllByType("button").find(node => node.props.title === `打开子线程对话：${child.title}`);
      act(() => button!.props.onClick());
      expect(opened).toEqual([child]);
      expect(embedded).toEqual([]);
    } finally { act(() => renderer?.unmount()); }
  });
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
    animationFrameDescriptors = [Object.getOwnPropertyDescriptor(globalThis, "requestAnimationFrame"),
      Object.getOwnPropertyDescriptor(globalThis, "cancelAnimationFrame")];
    Object.defineProperty(globalThis, "requestAnimationFrame", { configurable: true, value: (callback: FrameRequestCallback) => {
      const id = Number(setTimeout(() => { animationFrames.delete(id); callback(0); }, 0));
      animationFrames.add(id);
      return id;
    } });
    Object.defineProperty(globalThis, "cancelAnimationFrame", { configurable: true, value: (id: number) => {
      animationFrames.delete(id); clearTimeout(id);
    } });
    const selected = thread(1);
    const turn: TurnDetail = {
      turn_id: "turn_child_order", job_id: "job_child_order", session_id: "ses_parent",
      source_view_id: "view_child_order", source_view_revision: 3,
      ordinal: 1, root_logical_item_ordinal: 0, revision: 1, status: "completed", items_view: "full",
      created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:01Z",
      user_messages: [{ message_id: "child_user_message", content: "子线程输入", attachments: [], created_at: "2026-09-15T12:00:00Z" }],
      final_response: "最终响应", items: [],
      activity_stats: { duration_ms: 100, item_count: 4 },
      response_items: [
        { id: "child_before", kind: "reasoning", projection: "detail", status: "completed",
          source: { message_sequence: 1, item_id: "child_item_before", logical_item_ordinal: 1 }, text: "压缩前思考" },
        { id: "child_summary", kind: "compaction_summary", projection: "detail", status: "completed",
          source: { message_sequence: 2, item_id: "child_item_summary", logical_item_ordinal: 2 }, text: "压缩摘要" },
        { id: "child_after", kind: "reasoning", projection: "detail", status: "completed",
          source: { message_sequence: 3, item_id: "child_item_after", logical_item_ordinal: 3 }, text: "压缩后思考" },
        { id: "child_final", kind: "final_text", projection: "detail", status: "completed", final: true,
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
    let menu: ChatItemMenuRequest | undefined;
    const forkAt = (kind: string) => {
      const node = renderer!.root.find(node => typeof node.type === "string" && (
        kind === "user" ? node.props.className === "child-thread-user chat-user-text"
          : typeof node.props["data-response-sources"] === "string"
            && JSON.parse(node.props["data-response-sources"])[0].source.item_id === kind
      ));
      const source = { closest: () => null, getRootNode: () => ({}), ownerDocument: { getSelection: () => null } };
      node.props.onKeyDown({ key: "ContextMenu", target: source, currentTarget: source, preventDefault() {}, stopPropagation() {} });
      menu!.actions.find(action => action.id === "forkItem")!.run();
    };
    act(() => {
      renderer = create(<VirtuosoMockContext.Provider value={{ viewportHeight: 420, itemHeight: 100 }}><ChatItemMenuContext.Provider value={request => { menu = request; }}>
        <ChildThreadPanel {...props} threads={[selected]} conversation={conversation}
          onForkFromThread={(threadId, point) => { clicks.push({ threadId, point }); }} />
      </ChatItemMenuContext.Provider></VirtuosoMockContext.Provider>, { createNodeMock: () => ({
        offsetHeight: 420, offsetWidth: 400, scrollHeight: 420, scrollTop: 0, style: {},
        addEventListener: () => {}, removeEventListener: () => {},
        getBoundingClientRect: () => ({ width: 400, height: 420 }),
      }) });
    });
    const sources = () => renderer!.root.findAll((node) =>
      typeof node.type === "string" && typeof node.props["data-response-sources"] === "string",
    ).flatMap((node) => JSON.parse(node.props["data-response-sources"]));
    const expected = turn.response_items!.map(({ id, kind, source }) => ({ id, kind, source }));
    expect(sources()).toEqual([expected[1], expected[3]]);
    act(() => { forkAt("child_item_final"); });
    expect(clicks).toEqual([{ threadId: selected.thread_id, point: {
      sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, itemId: "child_item_final",
    } }]);
    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    await act(async () => { await toggle.props.onClick(); });
    expect(sources()).toEqual(expected);
    expect(renderer!.root.findAllByProps({ className: "chat-thinking-toggle" })).toHaveLength(1);
    act(() => {
      const item = renderer!.root.findByProps({ "data-chat-item-id": "child_after" });
      const source = { closest: () => null, getRootNode: () => ({}), ownerDocument: { getSelection: () => null } };
      item.props.onKeyDown({ key: "ContextMenu", target: source, currentTarget: source, preventDefault() {}, stopPropagation() {} });
      menu!.actions.find(action => action.id === "forkItem")!.run();
    });
    expect(clicks).toEqual([{ threadId: selected.thread_id, point: {
      sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, itemId: "child_item_final",
    } }, { threadId: selected.thread_id, point: {
      sourceViewId: turn.source_view_id, sourceViewRevision: turn.source_view_revision, itemId: "child_item_after",
    } }]);
    await act(async () => { await toggle.props.onClick(); });
    expect(sources()).toEqual([expected[1], expected[3]]);
    act(() => { forkAt("user"); });
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
      members: [
        { thread_id: "thr_main_canonical", role: "coordinator", work_mode: "write" },
        { thread_id: "thr_reviewer", role: "reviewer", work_mode: "read_only" },
      ],
      tasks: [{ task_id: "ttask_review", title: "核对 README", phase: "review", cycle: 2, assignee_thread_id: "thr_reviewer", status: "blocked", summary: "等待正文确认", error: undefined }],
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
    const emptyHtml = renderToStaticMarkup(<ChildThreadPanel {...panelProps()} mainThreadId={null} />);
    expect(emptyHtml).toContain("当前会话还没有子 Thread");

    const errorHtml = renderToStaticMarkup(
      <ChildThreadPanel
        {...panelProps()}
        error="请求失败 409 Conflict: 会话目录索引异常"
      />,
    );
    expect(errorHtml).toContain("会话线程加载失败");
    expect(errorHtml).toContain("会话目录索引异常");
    expect(errorHtml).not.toContain("当前会话还没有子 Thread");

    const noSessionHtml = renderToStaticMarkup(
      <ChildThreadPanel {...panelProps()} sessionId="" />,
    );
    expect(noSessionHtml).toContain("选择会话后查看其 Thread");
  });

  test("主 Thread 直接展示为列表项并打开对话", () => {
    const opened: string[] = [];
    const props = panelProps();
    let tree!: ReactTestRenderer;
    try {
      act(() => { tree = create(<ChildThreadPanel {...props}
        onOpenMainThread={() => opened.push(props.mainThreadId)} />); });
      expect(tree.root.findAllByProps({ "data-thread-id": props.mainThreadId })).toHaveLength(1);
      expect(tree.root.findAllByProps({ className: "empty-state" })).toHaveLength(0);
      act(() => tree.root.findByProps({ "aria-label": "打开主 Thread 对话" }).props.onClick());
      expect(opened).toEqual([props.mainThreadId]);
    } finally { act(() => tree?.unmount()); }
  });

  test("只在加载失败时显示重试按钮，会话缺失时禁用", () => {
    let refreshes = 0;
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({ onRefresh: () => { refreshes += 1; } })}
          threads={[thread(1)]}
          error="请求失败"
        />,
      );
    });

    const refreshButton = renderer!.root.findByProps({
      "aria-label": "刷新会话线程",
    });
    act(() => refreshButton.props.onClick());
    expect(refreshes).toBe(1);
    expect(refreshButton.props.disabled).toBe(false);
    renderer!.unmount();

    let disabledRenderer: ReactTestRenderer;
    act(() => {
      disabledRenderer = create(
        <ChildThreadPanel {...panelProps()} sessionId="" error="请求失败" />,
      );
    });
    const disabledButton = disabledRenderer!.root.findByProps({
      "aria-label": "刷新会话线程",
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
            onOpenMainThread: () => { selected.push("thr_main_canonical"); },
          })}
          mainThreadId={null}
        />,
      );
    });

    const mainRow = renderer!.root.findByProps({
      "aria-label": "打开主 Thread 对话",
    });
    expect(mainRow.props.disabled).toBe(true);
    act(() => mainRow.props.onClick());
    expect(selected).toEqual([]);
    renderer!.unmount();
  });

  test("Thread 列表只提供对话打开及资源操作，不提供调试跳转", () => {
    const selected: string[] = [];
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChildThreadPanel
          {...panelProps({
            onOpenMainThread: () => { selected.push("thr_main_canonical"); },
          })}
          threads={[thread(1), thread(2)]}
          activeThreadId="thr_child_2"
          onOpenThread={child => selected.push(child.thread_id)}
        />,
      );
    });

    const rows = renderer!.root.findAllByProps({ className: "child-thread-main" }).slice(1);
    expect(rows).toHaveLength(2);
    expect(renderer!.root.findAllByProps({ className: "codicon codicon-debug" })).toHaveLength(0);
    expect(renderer!.root.findAllByType("button").filter(node => String(node.props["aria-label"]).startsWith("调试子线程："))).toHaveLength(0);
    act(() => rows[0]!.props.onClick());
    const mainRow = renderer!.root.findByProps({
      "aria-label": "打开主 Thread 对话",
    });
    act(() => mainRow.props.onClick());
    expect(selected).toEqual(["thr_child_1", "thr_main_canonical"]);
    // 打开对话仍保留真实执行状态与当前编辑器的选中态。
    const statuses = renderer!.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.split(" ").includes("child-thread-status"),
    );
    expect(statuses.map((node) => textOf(node))).toEqual(["主线程", "运行中", "运行中"]);
    expect(rows[1]!.props["aria-current"]).toBe("page");
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
      expect(
        renderer!.root.findAllByProps({ className: "child-thread-notice-slot" }),
      ).toHaveLength(0);
      const copyButton = renderer!.root.findByProps({
        "aria-label": "复制 child thread ID: thr_child_1",
      });
      await act(async () => {
        copyButton.props.onClick();
        // 复制链路经过若干微任务，等待一个宏任务确保状态落地。
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(written).toEqual(["thr_child_1"]);
      const noticeSlot = renderer!.root.findByProps({
        className: "child-thread-notice-slot",
      });
      const notice = renderer!.root.findByProps({
        className: "child-thread-notice",
      });
      expect(textOf(notice)).toContain("已复制 child thread ID");
      expect(notice.props.title).toBe("已复制 child thread ID: thr_child_1");
      expect(noticeSlot.findByProps({ className: "child-thread-notice" })).toBeDefined();
      await act(async () => {
        renderer!.root.findByProps({ "aria-label": "关闭提示" }).props.onClick();
      });
      expect(
        renderer!.root.findAllByProps({ className: "child-thread-notice-slot" }),
      ).toHaveLength(0);
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
