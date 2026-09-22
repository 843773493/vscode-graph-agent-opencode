import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceNavigationNode } from "../../types/backend";
import type { SessionResourceExplorerController } from "../../hooks/session/useSessionResourceExplorer";
import { useSessionResourceTreeNavigation } from "./useSessionResourceTreeNavigation";

function navigationNode(
  nodeId: string,
  parentNodeId: string | null,
  position: number,
  name = nodeId,
): WorkspaceNavigationNode {
  return {
    node_id: nodeId,
    kind: "workspace_folder",
    name,
    parent_node_id: parentNodeId,
    workspace_id: null,
    position,
  } as unknown as WorkspaceNavigationNode;
}

interface HarnessHandle {
  navigation: ReturnType<typeof useSessionResourceTreeNavigation>;
}

function createHarness(options: {
  navigationNodes: WorkspaceNavigationNode[];
  explorer: Partial<SessionResourceExplorerController>;
  handleError: (prefix: string, error: unknown) => void;
  onSetWorkspaceParent?: (workspaceId: string, parentWorkspaceId: string | null) => Promise<void>;
  onRefreshWorkspaceSessions?: (workspaceId: string) => Promise<void>;
  onStatusChange?: (message: string) => void;
  onSelectSession?: (workspaceId: string, sessionId: string) => void | Promise<void>;
}): { renderer: ReactTestRenderer; handle: HarnessHandle } {
  const handle: HarnessHandle = { navigation: null as never };
  function Harness(): React.ReactNode {
    handle.navigation = useSessionResourceTreeNavigation({
      explorer: options.explorer as SessionResourceExplorerController,
      navigationNodes: options.navigationNodes,
      handleError: options.handleError,
      onSetWorkspaceParent: options.onSetWorkspaceParent ?? (async () => undefined),
      onRefreshWorkspaceSessions: options.onRefreshWorkspaceSessions ?? (async () => undefined),
      onStatusChange: options.onStatusChange ?? (() => undefined),
      onSelectSession: options.onSelectSession ?? (() => undefined),
    });
    return null;
  }
  let renderer: ReactTestRenderer;
  act(() => {
    renderer = create(<Harness />);
  });
  return { renderer: renderer!, handle };
}

interface FakeDragEvent {
  currentTarget: { getBoundingClientRect: () => { top: number; height: number } };
  clientY: number;
  dataTransfer: { effectAllowed: string; dropEffect: string; setData: (k: string, v: string) => void };
  stopPropagation: () => void;
  preventDefault: () => void;
}

function dragEvent(clientY = 0): FakeDragEvent {
  const store: Record<string, string> = {};
  return {
    currentTarget: { getBoundingClientRect: () => ({ top: 0, height: 40 }) },
    clientY,
    dataTransfer: {
      effectAllowed: "none",
      dropEffect: "none",
      setData: (key, value) => { store[key] = value; },
    },
    stopPropagation: () => undefined,
    preventDefault: () => undefined,
  };
}

describe("useSessionResourceTreeNavigation 导航层级派生", () => {
  test("按父节点分组并按 position、name 排序", () => {
    const { handle } = createHarness({
      navigationNodes: [
        navigationNode("b", null, 2),
        navigationNode("a", null, 1),
        navigationNode("c", null, 1, "aa"),
        navigationNode("child", "a", 1),
      ],
      explorer: {},
      handleError: () => undefined,
    });
    const rootIds = (handle.navigation.navigationChildren.get(null) ?? []).map((n) => n.node_id);
    expect(rootIds).toEqual(["a", "c", "b"]);
    expect((handle.navigation.navigationChildren.get("a") ?? []).map((n) => n.node_id)).toEqual(["child"]);
  });

  test("同 position 时按名称升序而不是插入顺序", () => {
    const { handle } = createHarness({
      navigationNodes: [
        navigationNode("z_first", null, 1, "zzz"),
        navigationNode("a_second", null, 1, "aaa"),
      ],
      explorer: {},
      handleError: () => undefined,
    });
    expect((handle.navigation.navigationChildren.get(null) ?? []).map((n) => n.node_id))
      .toEqual(["a_second", "z_first"]);
  });

  test("dropTargetClass 仅在目标键与放置区同时命中时返回类名", () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    expect(handle.navigation.dropTargetClass("workspace:n1")).toBe("");
  });
});

describe("useSessionResourceTreeNavigation 拖拽状态机", () => {
  test("startDrag 写入拖拽负载并清除放置目标，clearDrag 复位", () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    const event = dragEvent();
    act(() => {
      handle.navigation.startDrag(event as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
        parentNodeId: null,
      });
    });
    expect(handle.navigation.dragItem?.nodeId).toBe("cnode_1");
    expect(event.dataTransfer.effectAllowed).toBe("move");
    expect(event.dataTransfer.setData).toBeDefined();
    act(() => handle.navigation.clearDrag());
    expect(handle.navigation.dragItem).toBeNull();
  });

  test("不允许的放置清除拖拽并上报无法拖放", () => {
    const errors: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: (prefix) => errors.push(prefix),
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
        parentNodeId: null,
      });
    });
    act(() => {
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_2",
        sessionId: "ses_2",
        workspaceId: "gw_other",
      });
    });
    expect(errors).toEqual(["无法拖放"]);
    expect(handle.navigation.dragItem).toBeNull();
  });
});

interface PlacementCall {
  nodeId: string;
  parentNodeId: string | null;
  mode: string;
  targetNodeId?: string;
}

function flushDrop(): Promise<void> {
  return Promise.resolve().then(() => Promise.resolve()).then(() => Promise.resolve());
}

describe("useSessionResourceTreeNavigation 拖放提交", () => {
  test("父工作区更新失败时把导航位置回滚到原相对位置并上报拖放失败", async () => {
    const placements: PlacementCall[] = [];
    const errors: Array<{ prefix: string; message: string }> = [];
    const { handle } = createHarness({
      navigationNodes: [
        navigationNode("folder", null, 0),
        navigationNode("ws_node", "folder", 1),
        navigationNode("sibling", "folder", 2),
      ],
      explorer: {
        placeWorkspaceNode: async (nodeId, parentNodeId, mode, targetNodeId) => {
          placements.push({ nodeId, parentNodeId, mode, targetNodeId });
          return undefined as never;
        },
      },
      handleError: (prefix, error) => errors.push({
        prefix,
        message: error instanceof Error ? error.message : String(error),
      }),
      onSetWorkspaceParent: async () => {
        throw new Error("父工作区更新失败");
      },
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "workspace",
        nodeId: "ws_node",
        workspaceId: "gw_1",
        parentWorkspaceId: "gw_parent",
        parentNodeId: "folder",
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, { kind: "navigation_root" });
      await flushDrop();
    });
    expect(placements).toEqual([
      { nodeId: "ws_node", parentNodeId: null, mode: "last", targetNodeId: undefined },
      { nodeId: "ws_node", parentNodeId: "folder", mode: "before", targetNodeId: "sibling" },
    ]);
    expect(errors).toEqual([{ prefix: "拖放失败", message: "父工作区更新失败" }]);
  });

  test("回滚导航位置自身失败时附带恢复失败详情", async () => {
    let placementCalls = 0;
    const errors: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [navigationNode("ws_node", null, 0)],
      explorer: {
        placeWorkspaceNode: async () => {
          placementCalls += 1;
          if (placementCalls > 1) {
            throw new Error("导航服务不可用");
          }
          return undefined as never;
        },
      },
      handleError: (_prefix, error) => errors.push(
        error instanceof Error ? error.message : String(error),
      ),
      onSetWorkspaceParent: async () => {
        throw new Error("父工作区更新失败");
      },
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "workspace",
        nodeId: "ws_node",
        workspaceId: "gw_1",
        parentWorkspaceId: "gw_parent",
        parentNodeId: null,
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, { kind: "navigation_root" });
      await flushDrop();
    });
    expect(errors).toEqual([
      "父工作区更新失败；恢复工作区导航位置也失败: 导航服务不可用",
    ]);
  });

  test("源节点没有后继兄弟时回滚到父级末尾", async () => {
    const placements: PlacementCall[] = [];
    const { handle } = createHarness({
      navigationNodes: [navigationNode("ws_node", "folder", 0)],
      explorer: {
        placeWorkspaceNode: async (nodeId, parentNodeId, mode, targetNodeId) => {
          placements.push({ nodeId, parentNodeId, mode, targetNodeId });
          return undefined as never;
        },
      },
      handleError: () => undefined,
      onSetWorkspaceParent: async () => {
        throw new Error("父工作区更新失败");
      },
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "workspace",
        nodeId: "ws_node",
        workspaceId: "gw_1",
        parentWorkspaceId: "gw_parent",
        parentNodeId: "folder",
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, { kind: "navigation_root" });
      await flushDrop();
    });
    expect(placements).toEqual([
      { nodeId: "ws_node", parentNodeId: null, mode: "last", targetNodeId: undefined },
      { nodeId: "ws_node", parentNodeId: "folder", mode: "last", targetNodeId: undefined },
    ]);
  });

  test("会话拖到同一工作区内的会话时提交目录移动并刷新工作区会话", async () => {
    const moveCalls: unknown[][] = [];
    const refreshed: string[] = [];
    const statuses: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {
        moveCatalogNode: async (...args: unknown[]) => {
          moveCalls.push(args);
          return undefined as never;
        },
      },
      handleError: () => undefined,
      onRefreshWorkspaceSessions: async (workspaceId) => { refreshed.push(workspaceId); },
      onStatusChange: (message) => statuses.push(message),
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
        parentNodeId: null,
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_2",
        sessionId: "ses_2",
        workspaceId: "gw_1",
      });
      await flushDrop();
    });
    expect(moveCalls).toEqual([["gw_1", "cnode_1", "cnode_2", null]]);
    expect(refreshed).toEqual(["gw_1"]);
    expect(statuses).toEqual(["已移动会话"]);
  });

  test("会话已经位于目标文件夹下时拒绝且不提交目录移动", async () => {
    const moveCalls: unknown[][] = [];
    const errors: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {
        moveCatalogNode: async (...args: unknown[]) => {
          moveCalls.push(args);
          return undefined as never;
        },
      },
      handleError: (_prefix, error) => errors.push(
        error instanceof Error ? error.message : String(error),
      ),
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_child",
        sessionId: "ses_child",
        workspaceId: "gw_1",
        parentNodeId: "cnode_target",
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session_folder",
        nodeId: "cnode_target",
        workspaceId: "gw_1",
      });
      await flushDrop();
    });
    expect(errors).toEqual(["会话资源已经位于该位置"]);
    expect(moveCalls).toEqual([]);
  });

  test("同一次拖放重复投递 drop 时只提交一次目录移动", async () => {
    let moveCalls = 0;
    const statuses: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {
        moveCatalogNode: async () => {
          moveCalls += 1;
          await new Promise((resolve) => setTimeout(resolve, 5));
          return undefined as never;
        },
      },
      handleError: () => undefined,
      onStatusChange: (message) => statuses.push(message),
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
        parentNodeId: null,
      });
    });
    await act(async () => {
      // 重复 drop 事件：同步连发两次，第二次不得再发一次移动请求
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_2",
        sessionId: "ses_2",
        workspaceId: "gw_1",
      });
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_2",
        sessionId: "ses_2",
        workspaceId: "gw_1",
      });
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(moveCalls).toBe(1);
    expect(statuses).toEqual(["已移动会话"]);
  });

  test("目录移动成功但跟随刷新失败时报告刷新失败而不是整次拖放失败", async () => {
    const errors: Array<{ prefix: string; message: string }> = [];
    const statuses: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {
        moveCatalogNode: async () => undefined as never,
      },
      handleError: (prefix, error) => errors.push({
        prefix,
        message: error instanceof Error ? error.message : String(error),
      }),
      onStatusChange: (message) => statuses.push(message),
      onRefreshWorkspaceSessions: async () => {
        throw new Error("刷新会话列表失败");
      },
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
        parentNodeId: null,
      });
    });
    await act(async () => {
      handle.navigation.handleDrop(dragEvent() as never, {
        kind: "session",
        nodeId: "cnode_2",
        sessionId: "ses_2",
        workspaceId: "gw_1",
      });
      await flushDrop();
    });
    // 移动已成功：状态仍须报告已移动，失败只归因到跟随刷新这一步
    expect(statuses).toEqual(["已移动会话"]);
    expect(errors).toEqual([
      { prefix: "移动后刷新工作区会话列表失败", message: "刷新会话列表失败" },
    ]);
  });
});

describe("useSessionResourceTreeNavigation 拖拽悬停判定", () => {
  test("未开始拖拽时悬停不记录放置目标且不抛错", () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    const event = dragEvent();
    let stopped = false;
    event.stopPropagation = () => { stopped = true; };
    act(() => {
      handle.navigation.handleDragOver(event as never, { kind: "navigation_root" });
    });
    expect(handle.navigation.dropTargetClass("navigation_root")).toBe("");
    expect(stopped).toBe(false);
  });

  test("未开始拖拽时放置被忽略且不上报错误", () => {
    const errors: string[] = [];
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: (prefix) => errors.push(prefix),
    });
    act(() => {
      handle.navigation.handleDrop(dragEvent() as never, { kind: "navigation_root" });
    });
    expect(errors).toEqual([]);
    expect(handle.navigation.dragItem).toBeNull();
  });

  test("工作区拖到工作区上边缘时记录 before 放置区", () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "workspace",
        nodeId: "ws_node",
        workspaceId: "gw_1",
        parentWorkspaceId: null,
        parentNodeId: null,
      });
    });
    act(() => {
      handle.navigation.handleDragOver(dragEvent(2) as never, {
        kind: "workspace",
        nodeId: "ws_target",
        workspaceId: "gw_2",
        navigationParentNodeId: null,
        parentWorkspaceId: null,
      });
    });
    expect(handle.navigation.dropTargetClass("workspace:ws_target")).toBe(" drop-before");
  });

  test("工作区拖到会话上时判定为不允许且不标记放置目标", () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    act(() => {
      handle.navigation.startDrag(dragEvent() as never, {
        kind: "workspace",
        nodeId: "ws_node",
        workspaceId: "gw_1",
        parentWorkspaceId: null,
        parentNodeId: null,
      });
    });
    const event = dragEvent();
    act(() => {
      handle.navigation.handleDragOver(event as never, {
        kind: "session",
        nodeId: "cnode_1",
        sessionId: "ses_1",
        workspaceId: "gw_1",
      });
    });
    expect(event.dataTransfer.dropEffect).toBe("none");
    expect(handle.navigation.dropTargetClass("session:cnode_1")).toBe("");
  });
});

describe("useSessionResourceTreeNavigation 打开会话", () => {
  test("打开会话成功后清除进行中状态", async () => {
    const { handle } = createHarness({
      navigationNodes: [],
      explorer: {},
      handleError: () => undefined,
    });
    await act(async () => {
      handle.navigation.openSessionNode("gw_1", "ses_1", "gw_1:ses_1");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(handle.navigation.openingSession).toBeNull();
  });
});
