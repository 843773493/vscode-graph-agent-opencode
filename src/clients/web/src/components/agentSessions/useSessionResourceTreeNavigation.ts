import { useRef, useState, type DragEvent } from "react";
import type { WorkspaceNavigationNode } from "../../types/backend";
import type { SessionResourceExplorerController } from "../../hooks/session/useSessionResourceExplorer";
import {
  decideSessionResourceDrop,
  sessionResourceDropTargetKey,
  workspaceDropZoneForPointer,
  type SessionResourceDragItem,
  type SessionResourceDropTarget,
  type SessionResourceDropZone,
  type WorkspaceNavigationPlacement,
} from "./sessionResourceDrag";

interface SessionResourceTreeNavigationOptions {
  explorer: SessionResourceExplorerController;
  navigationNodes: WorkspaceNavigationNode[];
  handleError: (prefix: string, error: unknown) => void;
  onSetWorkspaceParent: (workspaceId: string, parentWorkspaceId: string | null) => Promise<void>;
  onRefreshWorkspaceSessions: (workspaceId: string) => Promise<void>;
  onStatusChange: (message: string) => void;
  onSelectSession: (workspaceId: string, sessionId: string) => void | Promise<void>;
}

/**
 * 会话资源浏览器的共享拖放机制：导航层级派生、拖拽/放置状态机，以及两条归属层级各自的
 * 放置提交链——Gateway 工作区导航链（performWorkspaceDrop）与会话目录链（performSessionDrop）。
 * 两条链的状态机共享，但提交动作与失败回滚各自独立，互不渗透。
 */
export function useSessionResourceTreeNavigation({
  explorer,
  navigationNodes,
  handleError,
  onSetWorkspaceParent,
  onRefreshWorkspaceSessions,
  onStatusChange,
  onSelectSession,
}: SessionResourceTreeNavigationOptions) {
  const [dragItem, setDragItem] = useState<SessionResourceDragItem | null>(null);
  const [dropTargetKey, setDropTargetKey] = useState<string | null>(null);
  const [dropTargetZone, setDropTargetZone] = useState<SessionResourceDropZone | null>(null);
  const openingSessionRequestSequenceRef = useRef(0);
  const [openingSession, setOpeningSession] = useState<{
    requestSequence: number;
    sessionKey: string;
  } | null>(null);

  const navigationChildren = new Map<string | null, WorkspaceNavigationNode[]>();
  for (const node of navigationNodes) {
    const parentId = node.parent_node_id ?? null;
    navigationChildren.set(parentId, [...(navigationChildren.get(parentId) ?? []), node]);
  }
  for (const children of navigationChildren.values()) {
    children.sort((left, right) => left.position - right.position || left.name.localeCompare(right.name));
  }

  const dropTargetClass = (targetKey: string): string => (
    dropTargetKey === targetKey && dropTargetZone
      ? ` drop-${dropTargetZone}`
      : ""
  );

  const clearDropTarget = () => {
    setDropTargetKey(null);
    setDropTargetZone(null);
  };

  const startDrag = (
    event: DragEvent<HTMLElement>,
    item: SessionResourceDragItem,
  ) => {
    setDragItem(item);
    setDropTargetKey(null);
    setDropTargetZone(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-boxteam-session-resource", JSON.stringify(item));
    event.dataTransfer.setData("text/plain", item.nodeId);
  };

  const clearDrag = () => {
    setDragItem(null);
    clearDropTarget();
  };

  const dropZoneForEvent = (
    event: DragEvent<HTMLElement>,
    target: SessionResourceDropTarget,
  ): SessionResourceDropZone => {
    if (
      (dragItem?.kind === "workspace" || dragItem?.kind === "workspace_folder")
      && (target.kind === "workspace" || target.kind === "workspace_folder")
    ) {
      const bounds = event.currentTarget.getBoundingClientRect();
      return workspaceDropZoneForPointer(event.clientY, bounds.top, bounds.height);
    }
    return "inside";
  };

  const handleDragOver = (
    event: DragEvent<HTMLElement>,
    target: SessionResourceDropTarget,
  ) => {
    if (!dragItem) {
      return;
    }
    event.stopPropagation();
    const zone = dropZoneForEvent(event, target);
    const decision = decideSessionResourceDrop(dragItem, target, zone);
    if (!decision.allowed) {
      event.dataTransfer.dropEffect = "none";
      setDropTargetKey(null);
      setDropTargetZone(null);
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDropTargetKey(sessionResourceDropTargetKey(target));
    setDropTargetZone(zone);
  };

  const placeWorkspaceNavigation = (
    nodeId: string,
    parentNodeId: string | null,
    placement: WorkspaceNavigationPlacement,
  ) => explorer.placeWorkspaceNode(
    nodeId,
    parentNodeId,
    placement.mode,
    placement.mode === "last" ? undefined : placement.targetNodeId,
  );

  const originalNavigationPlacement = (
    source: Extract<SessionResourceDragItem, { kind: "workspace" | "workspace_folder" }>,
  ): WorkspaceNavigationPlacement => {
    const siblings = navigationChildren.get(source.parentNodeId) ?? [];
    const sourceIndex = siblings.findIndex((node) => node.node_id === source.nodeId);
    const nextSibling = sourceIndex >= 0 ? siblings[sourceIndex + 1] : undefined;
    return nextSibling
      ? { mode: "before", targetNodeId: nextSibling.node_id }
      : { mode: "last" };
  };

  const restoreNavigationPlacement = async (
    source: Extract<SessionResourceDragItem, { kind: "workspace" | "workspace_folder" }>,
    error: unknown,
    rollbackPlacement: WorkspaceNavigationPlacement,
  ): Promise<never> => {
    try {
      await placeWorkspaceNavigation(source.nodeId, source.parentNodeId, rollbackPlacement);
    } catch (rollbackError) {
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}；恢复工作区导航位置也失败: ${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
      );
    }
    throw error;
  };

  const moveWorkspaceNavigation = async (
    source: Extract<SessionResourceDragItem, { kind: "workspace" | "workspace_folder" }>,
    parentNodeId: string | null,
    placement: WorkspaceNavigationPlacement,
  ) => {
    const rollbackPlacement = originalNavigationPlacement(source);
    await placeWorkspaceNavigation(source.nodeId, parentNodeId, placement);
    return rollbackPlacement;
  };

  const performWorkspaceDrop = async (
    source: Extract<SessionResourceDragItem, { kind: "workspace" | "workspace_folder" }>,
    target: SessionResourceDropTarget,
    zone: SessionResourceDropZone,
  ) => {
    const decision = decideSessionResourceDrop(source, target, zone);
    if (!decision.allowed) {
      throw new Error(decision.reason);
    }
    if (decision.action.kind === "move_workspace_navigation") {
      if (source.kind !== "workspace" && source.kind !== "workspace_folder") {
        throw new Error("拖放来源不是工作区或工作区文件夹");
      }
      const rollbackPlacement = await moveWorkspaceNavigation(
        source,
        decision.action.parentNodeId,
        decision.action.placement,
      );
      if (source.kind === "workspace" && source.parentWorkspaceId !== null) {
        try {
          await onSetWorkspaceParent(source.workspaceId, null);
        } catch (error) {
          await restoreNavigationPlacement(source, error, rollbackPlacement);
        }
      }
      onStatusChange(
        zone === "inside"
          ? source.kind === "workspace" ? "已移动工作区" : "已移动工作区文件夹"
          : source.kind === "workspace" ? "已调整工作区顺序" : "已调整工作区文件夹顺序",
      );
      return;
    }
    if (decision.action.kind === "set_workspace_parent") {
      if (source.kind !== "workspace") {
        throw new Error("拖放来源不是工作区");
      }
      const rollbackPlacement = await moveWorkspaceNavigation(
        source,
        decision.action.navigationParentNodeId,
        decision.action.placement,
      );
      if (source.parentWorkspaceId !== decision.action.parentWorkspaceId) {
        try {
          await onSetWorkspaceParent(source.workspaceId, decision.action.parentWorkspaceId);
        } catch (error) {
          await restoreNavigationPlacement(source, error, rollbackPlacement);
        }
      }
      onStatusChange(zone === "inside" ? "已设置子工作区" : "已调整子工作区顺序");
      return;
    }
    throw new Error("拖放来源不是工作区或工作区文件夹");
  };

  const performSessionDrop = async (
    source: Extract<SessionResourceDragItem, { kind: "session" | "session_folder" }>,
    target: SessionResourceDropTarget,
    zone: SessionResourceDropZone,
  ) => {
    const decision = decideSessionResourceDrop(source, target, zone);
    if (!decision.allowed) {
      throw new Error(decision.reason);
    }
    // 会话来源的决策只会产出 move_catalog_node；此处守卫同时满足 TS 判别联合收窄，
    // 并在决策契约一旦被改坏时立刻抛出而不是静默什么都不做。
    if (decision.action.kind !== "move_catalog_node") {
      throw new Error("会话拖放决策没有产出目录移动动作");
    }
    await explorer.moveCatalogNode(
      source.workspaceId,
      source.nodeId,
      decision.action.parentNodeId,
      source.parentNodeId,
    );
    await onRefreshWorkspaceSessions(source.workspaceId);
    onStatusChange(source.kind === "session" ? "已移动会话" : "已移动会话文件夹");
  };

  const performDrop = async (
    source: SessionResourceDragItem,
    target: SessionResourceDropTarget,
    zone: SessionResourceDropZone,
  ) => {
    if (source.kind === "session" || source.kind === "session_folder") {
      return performSessionDrop(source, target, zone);
    }
    return performWorkspaceDrop(source, target, zone);
  };

  const handleDrop = (
    event: DragEvent<HTMLElement>,
    target: SessionResourceDropTarget,
  ) => {
    if (!dragItem) {
      return;
    }
    event.stopPropagation();
    const source = dragItem;
    const zone = dropZoneForEvent(event, target);
    const decision = decideSessionResourceDrop(source, target, zone);
    if (!decision.allowed) {
      clearDrag();
      handleError("无法拖放", new Error(decision.reason));
      return;
    }
    event.preventDefault();
    clearDrag();
    void performDrop(source, target, zone).catch((error) => handleError("拖放失败", error));
  };

  /**
   * 打开会话并维护进行中状态：同一会话只保留最后一次请求的 loading，
   * 迟到的旧请求完成时不得清除当前请求的 loading。
   */
  const openSessionNode = (
    workspaceId: string,
    sessionId: string,
    sessionKey: string,
  ) => {
    const requestSequence = ++openingSessionRequestSequenceRef.current;
    setOpeningSession({ requestSequence, sessionKey });
    void Promise.resolve(onSelectSession(workspaceId, sessionId))
      .catch((error) => handleError("打开会话失败", error))
      .finally(() => {
        setOpeningSession((current) => (
          current?.requestSequence === requestSequence ? null : current
        ));
      });
  };

  return {
    navigationChildren,
    dragItem,
    dropTargetClass,
    clearDropTarget,
    startDrag,
    clearDrag,
    handleDragOver,
    handleDrop,
    openingSession,
    openSessionNode,
  };
}
