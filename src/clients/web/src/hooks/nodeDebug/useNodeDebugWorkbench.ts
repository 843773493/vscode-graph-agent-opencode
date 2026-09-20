import { useCallback } from "react";

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
  sessionId: string | null;
  enabled: boolean;
  onStatusChange: (message: string) => void;
  extensionWindowRequested: boolean;
  setAuxiliaryVisible: (visible: boolean) => void;
  setAuxiliaryTab: (tab: "debug") => void;
  persistLayoutSettings: (layout: WebUiSettingsUpdate["layout"]) => void;
}

export interface NodeDebugWorkbenchBinding {
  threadId: string;
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
  sessionId,
  enabled,
  onStatusChange,
  extensionWindowRequested,
  setAuxiliaryVisible,
  setAuxiliaryTab,
  persistLayoutSettings,
}: UseNodeDebugWorkbenchOptions): NodeDebugWorkbenchBinding {
  const {
    threadId,
    selectThread: selectOwnerThread,
  } = useNodeDebugOwner({ workspaceId, sessionId });
  const controller = useNodeDebugController({
    apiPort,
    workspaceId,
    sessionId,
    threadId,
    enabled,
    onStatusChange,
  });

  const selectThread = useCallback((nextThreadId: string) => {
    selectOwnerThread(nextThreadId);
    setAuxiliaryVisible(true);
    setAuxiliaryTab("debug");
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "debug" });
    }
    onStatusChange(nextThreadId === "main" ? "已切换到主线程调试" : `已切换调试 owner: ${nextThreadId}`);
  }, [extensionWindowRequested, onStatusChange, persistLayoutSettings, selectOwnerThread, setAuxiliaryTab, setAuxiliaryVisible]);

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
    controller,
    activeFrame: controller.state?.call_stack?.[0] ?? null,
    selectThread,
    changeBreakpoint,
  };
}
