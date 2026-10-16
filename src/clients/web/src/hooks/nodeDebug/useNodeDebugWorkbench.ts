import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getSessionMainThread } from "../../api";
import { errorMessage } from "../../utils/errorMessage";

import { requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import type { NodeDebugBreakpointDefinition } from "../../components/nodeDebug/NodeDebugBreakpointGutter";
import type { NodeDebugState, WebUiSettingsUpdate } from "../../types/backend";
import {
  useNodeDebugController,
  type NodeDebugController,
} from "./useNodeDebugController";
import { useNodeDebugOwner } from "./useNodeDebugOwner";

interface UseNodeDebugWorkbenchOptions {
  apiPort: number;
  workspaceId: string | null;
  workspaces: readonly GatewayWorkspaceRequestTarget[];
  sessionId: string | null;
  enabled: boolean;
  onStatusChange: (message: string) => void;
  extensionWindowRequested: boolean;
  setAuxiliaryVisible: (visible: boolean) => void;
  setAuxiliaryTab: (tab: "debug") => void;
  persistLayoutSettings: (layout: WebUiSettingsUpdate["layout"]) => void;
}

export interface NodeDebugWorkbenchBinding {
  threadId: string | null;
  mainThreadId: string | null;
  controller: NodeDebugController;
  activeFrame: NonNullable<NodeDebugState["call_stack"]>[number] | null;
  selectThread: (threadId: string) => void;
  changeBreakpoint: (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => void;
}

export function useNodeDebugWorkbench({
  apiPort,
  workspaceId,
  workspaces,
  sessionId,
  enabled,
  onStatusChange,
  extensionWindowRequested,
  setAuxiliaryVisible,
  setAuxiliaryTab,
  persistLayoutSettings,
}: UseNodeDebugWorkbenchOptions): NodeDebugWorkbenchBinding {
  const [resolvedMainThread, setResolvedMainThread] = useState<{
    workspaceId: string;
    sessionId: string;
    threadId: string;
  } | null>(null);
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  const selectedWorkspace = workspaces.find((workspace) => workspace.workspace_id === workspaceId);
  const backendWorkspaceId = selectedWorkspace?.backend_workspace_id;
  const connectionKind = selectedWorkspace?.connection_kind;
  const requestTarget = useMemo<GatewayWorkspaceRequestTarget | null>(() => (
    workspaceId && connectionKind
      ? { workspace_id: workspaceId, backend_workspace_id: backendWorkspaceId, connection_kind: connectionKind }
      : null
  ), [workspaceId, backendWorkspaceId, connectionKind]);
  useEffect(() => {
    if (!sessionId || !workspaceId) {
      setResolvedMainThread(null);
      return;
    }
    let current = true;
    setResolvedMainThread(null);
    void Promise.resolve().then(() => getSessionMainThread(
      apiPort, sessionId,
      requireGatewayWorkspaceRequestTarget(requestTarget ? [requestTarget] : [], workspaceId),
    ))
      .then((owner) => {
        if (current) setResolvedMainThread({ workspaceId, sessionId, threadId: owner.thread_id });
      })
      .catch((cause: unknown) => {
        if (current) onStatusChangeRef.current(`解析调试主线程失败: ${errorMessage(cause)}`);
      });
    return () => { current = false; };
  }, [apiPort, sessionId, workspaceId, requestTarget]);

  const resolvedMainThreadId = resolvedMainThread?.workspaceId === workspaceId
    && resolvedMainThread.sessionId === sessionId
    ? resolvedMainThread.threadId : null;

  const {
    threadId,
    mainThreadId,
    selectThread: selectOwnerThread,
  } = useNodeDebugOwner({ workspaceId, sessionId }, resolvedMainThreadId);
  const controller = useNodeDebugController({
    apiPort,
    workspaceId,
    sessionId,
    threadId: threadId ?? "",
    enabled: enabled && threadId !== null,
    onStatusChange,
  });

  const selectThread = useCallback((nextThreadId: string) => {
    selectOwnerThread(nextThreadId);
    setAuxiliaryVisible(true);
    setAuxiliaryTab("debug");
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "debug" });
    }
    onStatusChange(nextThreadId === mainThreadId ? "已切换到主线程调试" : `已切换调试 owner: ${nextThreadId}`);
  }, [extensionWindowRequested, mainThreadId, onStatusChange, persistLayoutSettings, selectOwnerThread, setAuxiliaryTab, setAuxiliaryVisible]);

  const changeBreakpoint = useCallback((
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ): void => {
    if (!definition) {
      if (breakpointId) {
        void controller.runAction({
          action: "clear_breakpoint",
          params: { breakpoint_id: breakpointId },
        });
      }
      return;
    }
    if (breakpointId) {
      void controller.runAction({
        action: "update_breakpoint",
        params: {
          breakpoint_id: breakpointId,
          path,
          line,
          condition: definition.condition,
          hit_condition: definition.hit_condition,
          log_message: definition.log_message,
        },
      });
      return;
    }
    void controller.runAction({
      action: "set_breakpoint",
      params: {
        path,
        line,
        condition: definition.condition,
        hit_condition: definition.hit_condition,
        log_message: definition.log_message,
      },
    });
  }, [controller]);

  return {
    threadId,
    mainThreadId,
    controller,
    activeFrame: controller.state?.call_stack?.[0] ?? null,
    selectThread,
    changeBreakpoint,
  };
}
