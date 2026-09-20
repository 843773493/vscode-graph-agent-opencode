import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  activateNodeDebugConfiguration,
  applyNodeDebugAction,
  createNodeDebugConfiguration,
  deleteNodeDebugConfiguration,
  getNodeDebugCapabilities,
  getNodeDebugState,
  startNodeDebug,
  updateNodeDebugConfiguration,
} from "../../api";
import type {
  NodeDebugActionRequest,
  NodeDebugCapabilities,
  NodeDebugState,
} from "../../types/backend";
import {
  NodeDebugMutationGate,
  type NodeDebugMutation,
} from "./nodeDebugMutationGate";
import { createNodeDebugSyncChannel } from "./nodeDebugSync";

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
  const [state, setState] = useState<NodeDebugState | null>(null);
  const [capabilities, setCapabilities] = useState<NodeDebugCapabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const ownerKey = `${apiPort}:${workspaceId ?? ""}:${sessionId ?? ""}:${threadId}`;
  const pollGenerationRef = useRef(0);
  const mutationGateRef = useRef<NodeDebugMutationGate | null>(null);
  if (mutationGateRef.current === null) {
    mutationGateRef.current = new NodeDebugMutationGate(ownerKey);
  }
  const mutationGate = mutationGateRef.current;
  const syncChannelRef = useRef<ReturnType<typeof createNodeDebugSyncChannel> | null>(null);
  const stateRequestsRef = useRef<Map<string, Promise<NodeDebugState>>>(new Map());
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const syncMutationFlags = () => {
    const busyFlags = mutationGate.busyFlags(enabledRef.current);
    setActionBusy(busyFlags.actionBusy);
    setLoading(busyFlags.loading);
  };

  // owner 切换必须先使旧 mutation 失效，再允许新 owner 的轮询落地；否则旧
  // owner 的异步响应可能在切换后覆盖当前调试状态。旧 mutation 的锁仍保留
  // 到请求 settle，避免切回同一 owner 时重复发起后端 mutation。
  useLayoutEffect(() => {
    if (mutationGate.switchOwner(ownerKey)) {
      setState(null);
      setCapabilities(null);
      setError(null);
    }
    syncMutationFlags();
  }, [enabled, ownerKey]);

  const beginMutation = (mode: NodeDebugMutation["mode"]): NodeDebugMutation | null => {
    const mutation = mutationGate.beginMutation(ownerKey, mode);
    if (mutation) syncMutationFlags();
    return mutation;
  };

  const isCurrentMutation = (
    mutationOwnerKey: string,
    mutationOwnerGeneration: number,
    mutationGeneration: number,
  ): boolean => mutationGate.isCurrentMutation({
    ownerKey: mutationOwnerKey,
    ownerGeneration: mutationOwnerGeneration,
    mutationGeneration,
  });

  const loadState = useCallback((force = false): Promise<NodeDebugState> => {
    if (!sessionId) {
      return Promise.reject(new Error("当前没有可读取调试状态的会话"));
    }
    const requestKey = `${apiPort}:${workspaceId ?? ""}:${sessionId}:${threadId}`;
    if (!force) {
      const inFlight = stateRequestsRef.current.get(requestKey);
      if (inFlight) {
        return inFlight;
      }
    }
    const request = getNodeDebugState(apiPort, sessionId, threadId, workspaceId);
    stateRequestsRef.current.set(requestKey, request);
    void request.then(() => {
      if (stateRequestsRef.current.get(requestKey) === request) {
        stateRequestsRef.current.delete(requestKey);
      }
    }, () => {
      if (stateRequestsRef.current.get(requestKey) === request) {
        stateRequestsRef.current.delete(requestKey);
      }
    });
    return request;
  }, [apiPort, sessionId, threadId, workspaceId]);

  const refresh = useCallback(async (force = false) => {
    const refreshOwnerKey = ownerKey;
    const refreshSnapshot = mutationGate.captureSnapshot(refreshOwnerKey);
    if (!enabledRef.current || !sessionId) {
      if (mutationGate.isCurrentOwner(refreshOwnerKey, refreshSnapshot.ownerGeneration)) {
        setState(null);
      }
      return;
    }
    if (mutationGate.hasMutation(refreshOwnerKey)) return;
    const nextState = await loadState(force);
    if (
      mutationGate.isCurrentSnapshot(refreshSnapshot, true)
    ) {
      setState(nextState);
    }
  }, [loadState, ownerKey, sessionId]);

  const releaseMutation = (mutation: NodeDebugMutation) => {
    const result = mutationGate.releaseMutation(mutation);
    if (!result.released || !result.isCurrentOwner) return;
    syncMutationFlags();
    // 旧 owner 的响应被 generation 丢弃后，必须重新读取后端权威状态。
    if (!result.wasCurrent && enabledRef.current) void refresh(true);
  };

  useEffect(() => {
    const channel = createNodeDebugSyncChannel(
      workspaceId,
      sessionId,
      threadId,
      () => void refresh(),
    );
    syncChannelRef.current = channel;
    return () => {
      if (syncChannelRef.current === channel) syncChannelRef.current = null;
      channel.close();
    };
  }, [refresh, sessionId, threadId, workspaceId]);

  const publishStateChange = useCallback(() => {
    syncChannelRef.current?.publish();
  }, []);

  const refreshAfterMutationFailure = useCallback(async (
    message: string,
    mutationOwnerKey: string,
    mutationOwnerGeneration: number,
    mutationGeneration: number,
  ) => {
    try {
      const authoritativeState = await loadState(true);
      if (
        isCurrentMutation(
          mutationOwnerKey,
          mutationOwnerGeneration,
          mutationGeneration,
        )
      ) {
        setState(authoritativeState);
      }
    } catch (refreshCause: unknown) {
      const refreshMessage = refreshCause instanceof Error
        ? refreshCause.message
        : String(refreshCause);
      if (
        isCurrentMutation(
          mutationOwnerKey,
          mutationOwnerGeneration,
          mutationGeneration,
        )
      ) {
        setError(`${message}；重新获取调试状态失败: ${refreshMessage}`);
      }
    }
  }, [loadState]);

  useEffect(() => {
    const effectOwnerKey = ownerKey;
    const effectOwnerGeneration = mutationGate.ownerGeneration;
    const pollGeneration = ++pollGenerationRef.current;
    setError(null);
    setState(null);
    if (!enabled) {
      setCapabilities(null);
      syncMutationFlags();
      return;
    }
    syncMutationFlags();
    let disposed = false;

    const poll = async () => {
      const pollMutationGeneration = mutationGate.mutationGeneration;
      const pollSnapshot = {
        ownerKey: effectOwnerKey,
        ownerGeneration: effectOwnerGeneration,
        mutationGeneration: pollMutationGeneration,
      };
      try {
        const [nextState, nextCapabilities] = await Promise.all([
          sessionId
            ? loadState()
            : Promise.resolve(null),
          getNodeDebugCapabilities(apiPort, workspaceId),
        ]);
        if (
          !disposed
          && pollGenerationRef.current === pollGeneration
          && mutationGate.isCurrentSnapshot(pollSnapshot, true)
        ) {
          setState(nextState);
          setCapabilities(nextCapabilities);
        }
      } catch (cause: unknown) {
        if (
          !disposed
          && pollGenerationRef.current === pollGeneration
          && mutationGate.isCurrentSnapshot(pollSnapshot, true)
        ) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => {
      if (!sessionId) return;
      const pollMutationGeneration = mutationGate.mutationGeneration;
      const pollSnapshot = {
        ownerKey: effectOwnerKey,
        ownerGeneration: effectOwnerGeneration,
        mutationGeneration: pollMutationGeneration,
      };
      void loadState()
        .then((nextState) => {
          if (
            !disposed
            && pollGenerationRef.current === pollGeneration
            && mutationGate.isCurrentSnapshot(pollSnapshot, true)
          ) {
            setState(nextState);
          }
        })
        .catch((cause: unknown) => {
          if (
            !disposed
            && pollGenerationRef.current === pollGeneration
            && mutationGate.isCurrentSnapshot(pollSnapshot, true)
          ) {
            setError(cause instanceof Error ? cause.message : String(cause));
          }
        });
    }, 800);

    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [apiPort, enabled, loadState, ownerKey, sessionId, threadId, workspaceId]);

  const runAction = useCallback(async (
    action: NodeDebugActionRequest["action"],
    params: Record<string, unknown> = {},
  ): Promise<NodeDebugState | null> => {
    if (!enabled || !sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
    setError(null);
    try {
      const nextState = await applyNodeDebugAction(
        apiPort,
        { session_id: sessionId, thread_id: threadId, action, params },
        workspaceId,
      );
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`源码调试：${action}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`源码调试动作失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
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
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
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
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已启动源码调试: ${path}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`启动源码调试失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
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
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
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
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已创建调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`创建调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const activateConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
    setError(null);
    try {
      const nextState = await activateNodeDebugConfiguration(
        apiPort,
        configurationId,
        { session_id: sessionId, thread_id: threadId },
        workspaceId,
      );
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已切换调试方案: ${nextState.active_configuration_name ?? configurationId}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`切换调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
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
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
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
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange(`已保存调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`保存调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
      return null;
    } finally {
      releaseMutation(mutation);
    }
  }, [apiPort, onStatusChange, ownerKey, publishStateChange, refreshAfterMutationFailure, sessionId, threadId, workspaceId]);

  const deleteConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    const mutation = beginMutation("action");
    if (!mutation) return null;
    const {
      ownerKey: mutationOwnerKey,
      ownerGeneration: mutationOwnerGeneration,
      mutationGeneration,
    } = mutation;
    setError(null);
    try {
      const nextState = await deleteNodeDebugConfiguration(
        apiPort,
        sessionId,
        threadId,
        configurationId,
        workspaceId,
      );
      if (!isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        return null;
      }
      setState(nextState);
      publishStateChange();
      onStatusChange("已删除调试方案");
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (isCurrentMutation(mutationOwnerKey, mutationOwnerGeneration, mutationGeneration)) {
        setError(message);
        onStatusChange(`删除调试方案失败: ${message}`);
      }
      await refreshAfterMutationFailure(
        message,
        mutationOwnerKey,
        mutationOwnerGeneration,
        mutationGeneration,
      );
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
