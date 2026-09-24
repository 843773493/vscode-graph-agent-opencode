import { useCallback, useRef } from "react";
import { errorMessage } from "../../utils/errorMessage";

import {
  activateNodeDebugConfiguration,
  applyNodeDebugAction,
  createNodeDebugConfiguration,
  deleteNodeDebugConfiguration,
  startNodeDebug,
  updateNodeDebugConfiguration,
} from "../../api";
import type {
  NodeDebugActionCommand,
  NodeDebugActionRequest,
  NodeDebugState,
} from "../../types/backend";
import {
  NodeDebugMutationGate,
  type NodeDebugMutation,
} from "./nodeDebugMutationGate";
import { useNodeDebugStateSync } from "./useNodeDebugStateSync";

interface UseNodeDebugControllerOptions {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
  enabled: boolean;
  onStatusChange: (message: string) => void;
}

interface StartNodeDebugOptions {
  path: string;
  workingDirectory?: string | null;
  launchProfileName?: string | null;
  configurationId?: string | null;
  args?: string[];
}

export function useNodeDebugController({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  enabled,
  onStatusChange,
}: UseNodeDebugControllerOptions) {
  const ownerKey = `${apiPort}:${workspaceId ?? ""}:${sessionId ?? ""}:${threadId}`;
  const mutationGateRef = useRef<NodeDebugMutationGate | null>(null);
  if (mutationGateRef.current === null) {
    mutationGateRef.current = new NodeDebugMutationGate(ownerKey);
  }
  const mutationGate = mutationGateRef.current;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const {
    state,
    capabilities,
    error,
    loading,
    actionBusy,
    setState,
    setError,
    refresh,
    refreshAfterMutationFailure,
    publishStateChange,
    syncMutationFlags,
  } = useNodeDebugStateSync({
    apiPort,
    workspaceId,
    sessionId,
    threadId,
    ownerKey,
    enabled,
    mutationGate,
  });

  const beginMutation = (mode: NodeDebugMutation["mode"]): NodeDebugMutation | null => {
    const mutation = mutationGate.beginMutation(ownerKey, mode);
    if (mutation) syncMutationFlags();
    return mutation;
  };

  const isCurrentMutation = (mutation: NodeDebugMutation): boolean => (
    mutationGate.isCurrentMutation(mutation)
  );

  const releaseMutation = (mutation: NodeDebugMutation) => {
    const result = mutationGate.releaseMutation(mutation);
    if (!result.released || !result.isCurrentOwner) return;
    syncMutationFlags();
    // 旧 owner 的响应被 generation 丢弃后，必须重新读取后端权威状态。
    if (!result.wasCurrent && enabledRef.current) void refresh(true);
  };

  const runAction = useCallback(async (
    command: NodeDebugActionCommand,
  ): Promise<NodeDebugState | null> => {
    if (!enabled || !sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    setError(null);
    try {
      const payload: NodeDebugActionRequest = {
        ...command,
        session_id: sessionId,
        thread_id: threadId,
      };
      const nextState = await applyNodeDebugAction(
        apiPort,
        payload,
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`源码调试：${command.action}`);
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`源码调试动作失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, enabled, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const start = useCallback(async ({
    path,
    workingDirectory,
    launchProfileName,
    configurationId,
    args = [],
  }: StartNodeDebugOptions): Promise<NodeDebugState | null> => {
    if (!sessionId) return null;
    const mutation = beginMutation("loading");
    if (!mutation) return null;
    setError(null);
    try {
      const nextState = await startNodeDebug(
        apiPort,
        {
          session_id: sessionId,
          thread_id: threadId,
          configuration_id: configurationId,
          path,
          working_directory: workingDirectory,
          launch_profile_name: launchProfileName,
          args,
        },
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已启动源码调试: ${path}`);
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`启动源码调试失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const createConfiguration = useCallback(async (input: {
    name: string;
    path: string | null;
    workingDirectory: string;
    launchProfileName: string | null;
    args: string[];
  }): Promise<NodeDebugState | null> => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    setError(null);
    try {
      const nextState = await createNodeDebugConfiguration(
        apiPort,
        {
          session_id: sessionId,
          thread_id: threadId,
          name: input.name,
          script_path: input.path,
          working_directory: input.workingDirectory,
          launch_profile_name: input.launchProfileName,
          args: input.args,
          activate: true,
        },
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已创建调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`创建调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const activateConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    setError(null);
    try {
      const nextState = await activateNodeDebugConfiguration(
        apiPort,
        configurationId,
        { session_id: sessionId, thread_id: threadId },
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已切换调试方案: ${nextState.active_configuration_name ?? configurationId}`);
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`切换调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const updateConfiguration = useCallback(async (input: {
    configurationId: string;
    name: string;
    path: string | null;
    workingDirectory: string;
    launchProfileName: string | null;
    args: string[];
    breakpoints: Array<{
      path: string;
      line: number;
      column?: number;
      condition?: string | null;
      hit_condition?: number | null;
      log_message?: string | null;
    }>;
  }) => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    setError(null);
    try {
      const nextState = await updateNodeDebugConfiguration(
        apiPort,
        input.configurationId,
        {
          session_id: sessionId,
          thread_id: threadId,
          name: input.name,
          script_path: input.path,
          working_directory: input.workingDirectory,
          launch_profile_name: input.launchProfileName,
          args: input.args,
          breakpoints: input.breakpoints,
        },
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已保存调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`保存调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const deleteConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    setError(null);
    try {
      const nextState = await deleteNodeDebugConfiguration(
        apiPort,
        sessionId,
        threadId,
        configurationId,
        workspaceId,
      );
      if (!isCurrentMutation(mutation)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange("已删除调试方案");
      return nextState;
    } catch (cause: unknown) {
      const message = errorMessage(cause);
      if (isCurrentMutation(mutation)) {
        setError(message);
        onStatusChange(`删除调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(message, mutation);
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  return {
    state,
    capabilities,
    error,
    loading,
    actionBusy,
    refresh,
    runAction,
    start,
    createConfiguration,
    updateConfiguration,
    activateConfiguration,
    deleteConfiguration,
  };
}

export type NodeDebugController = ReturnType<typeof useNodeDebugController>;
