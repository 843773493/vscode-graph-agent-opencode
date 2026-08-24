import { useCallback, useEffect, useRef, useState } from "react";

import {
  activateNodeDebugConfiguration,
  applyNodeDebugAction,
  createNodeDebugConfiguration,
  deleteNodeDebugConfiguration,
  getNodeDebugCapabilities,
  getNodeDebugState,
  startNodeDebug,
  updateNodeDebugConfiguration,
} from "../api";
import type {
  NodeDebugActionRequest,
  NodeDebugCapabilities,
  NodeDebugState,
} from "../types/backend";
import { createNodeDebugSyncChannel } from "./nodeDebugSync";

interface UseNodeDebugControllerOptions {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
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
  enabled,
  onStatusChange,
}: UseNodeDebugControllerOptions) {
  const [state, setState] = useState<NodeDebugState | null>(null);
  const [capabilities, setCapabilities] = useState<NodeDebugCapabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const requestVersionRef = useRef(0);
  const actionInFlightRef = useRef(false);
  const syncChannelRef = useRef<ReturnType<typeof createNodeDebugSyncChannel> | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled || !sessionId) {
      setState(null);
      return;
    }
    const requestVersion = requestVersionRef.current;
    const nextState = await getNodeDebugState(apiPort, sessionId, workspaceId);
    if (
      requestVersion === requestVersionRef.current
      && !actionInFlightRef.current
    ) {
      setState(nextState);
    }
  }, [apiPort, enabled, sessionId, workspaceId]);

  useEffect(() => {
    const channel = createNodeDebugSyncChannel(
      workspaceId,
      sessionId,
      () => void refresh(),
    );
    syncChannelRef.current = channel;
    return () => {
      if (syncChannelRef.current === channel) syncChannelRef.current = null;
      channel.close();
    };
  }, [refresh, sessionId, workspaceId]);

  const publishStateChange = useCallback(() => {
    syncChannelRef.current?.publish();
  }, []);

  useEffect(() => {
    const requestVersion = ++requestVersionRef.current;
    setError(null);
    setState(null);
    if (!enabled) {
      setCapabilities(null);
      actionInFlightRef.current = false;
      return;
    }
    actionInFlightRef.current = false;
    let disposed = false;

    const poll = async () => {
      try {
        const [nextState, nextCapabilities] = await Promise.all([
          sessionId
            ? getNodeDebugState(apiPort, sessionId, workspaceId)
            : Promise.resolve(null),
          getNodeDebugCapabilities(apiPort, workspaceId),
        ]);
        if (
          !disposed
          && requestVersionRef.current === requestVersion
          && !actionInFlightRef.current
        ) {
          setState(nextState);
          setCapabilities(nextCapabilities);
        }
      } catch (cause: unknown) {
        if (!disposed) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => {
      if (!sessionId) return;
      void getNodeDebugState(apiPort, sessionId, workspaceId)
        .then((nextState) => {
          if (
            !disposed
            && requestVersionRef.current === requestVersion
            && !actionInFlightRef.current
          ) {
            setState(nextState);
          }
        })
        .catch((cause: unknown) => {
          if (!disposed) {
            setError(cause instanceof Error ? cause.message : String(cause));
          }
        });
    }, 800);

    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [apiPort, enabled, sessionId, workspaceId]);

  const runAction = useCallback(async (
    action: NodeDebugActionRequest["action"],
    params: Record<string, unknown> = {},
  ): Promise<NodeDebugState | null> => {
    if (!enabled || !sessionId) return null;
    const actionVersion = ++requestVersionRef.current;
    actionInFlightRef.current = true;
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await applyNodeDebugAction(
        apiPort,
        { session_id: sessionId, action, params },
        workspaceId,
      );
      if (actionVersion === requestVersionRef.current) setState(nextState);
      publishStateChange();
      onStatusChange(`源码调试：${action}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`源码调试动作失败: ${message}`);
      try {
        const authoritativeState = await getNodeDebugState(apiPort, sessionId, workspaceId);
        if (actionVersion === requestVersionRef.current) setState(authoritativeState);
      } catch (refreshCause: unknown) {
        const refreshMessage = refreshCause instanceof Error
          ? refreshCause.message
          : String(refreshCause);
        setError(`${message}；重新获取调试状态失败: ${refreshMessage}`);
      }
      return null;
    } finally {
      actionInFlightRef.current = false;
      setActionBusy(false);
      if (requestVersionRef.current === actionVersion) {
        requestVersionRef.current += 1;
      }
    }
  }, [apiPort, enabled, onStatusChange, publishStateChange, sessionId, workspaceId]);

  const start = useCallback(async ({
    path,
    workingDirectory,
    launchProfileName,
    configurationId,
    args = [],
  }: StartNodeDebugOptions): Promise<NodeDebugState | null> => {
    if (!sessionId) return null;
    const actionVersion = ++requestVersionRef.current;
    actionInFlightRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const nextState = await startNodeDebug(
        apiPort,
        {
          session_id: sessionId,
          configuration_id: configurationId,
          path,
          working_directory: workingDirectory,
          launch_profile_name: launchProfileName,
          args,
        },
        workspaceId,
      );
      if (actionVersion === requestVersionRef.current) setState(nextState);
      publishStateChange();
      onStatusChange(`已启动源码调试: ${path}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`启动源码调试失败: ${message}`);
      return null;
    } finally {
      actionInFlightRef.current = false;
      setLoading(false);
      if (requestVersionRef.current === actionVersion) {
        requestVersionRef.current += 1;
      }
    }
  }, [apiPort, onStatusChange, publishStateChange, sessionId, workspaceId]);

  const createConfiguration = useCallback(async (input: {
    name: string;
    path: string | null;
    workingDirectory: string;
    launchProfileName: string | null;
    args: string[];
  }): Promise<NodeDebugState | null> => {
    if (!sessionId) return null;
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await createNodeDebugConfiguration(
        apiPort,
        {
          session_id: sessionId,
          name: input.name,
          script_path: input.path,
          working_directory: input.workingDirectory,
          launch_profile_name: input.launchProfileName,
          args: input.args,
          activate: true,
        },
        workspaceId,
      );
      setState(nextState);
      publishStateChange();
      onStatusChange(`已创建调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`创建调试方案失败: ${message}`);
      return null;
    } finally {
      setActionBusy(false);
    }
  }, [apiPort, onStatusChange, publishStateChange, sessionId, workspaceId]);

  const activateConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await activateNodeDebugConfiguration(
        apiPort,
        sessionId,
        configurationId,
        workspaceId,
      );
      setState(nextState);
      publishStateChange();
      onStatusChange(`已切换调试方案: ${nextState.active_configuration_name ?? configurationId}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`切换调试方案失败: ${message}`);
      return null;
    } finally {
      setActionBusy(false);
    }
  }, [apiPort, onStatusChange, publishStateChange, sessionId, workspaceId]);

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
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await updateNodeDebugConfiguration(
        apiPort,
        input.configurationId,
        {
          session_id: sessionId,
          name: input.name,
          script_path: input.path,
          working_directory: input.workingDirectory,
          launch_profile_name: input.launchProfileName,
          args: input.args,
          breakpoints: input.breakpoints,
        },
        workspaceId,
      );
      setState(nextState);
      publishStateChange();
      onStatusChange(`已保存调试方案: ${input.name}`);
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`保存调试方案失败: ${message}`);
      return null;
    } finally {
      setActionBusy(false);
    }
  }, [apiPort, onStatusChange, publishStateChange, sessionId, workspaceId]);

  const deleteConfiguration = useCallback(async (configurationId: string) => {
    if (!sessionId) return null;
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await deleteNodeDebugConfiguration(
        apiPort,
        sessionId,
        configurationId,
        workspaceId,
      );
      setState(nextState);
      publishStateChange();
      onStatusChange("已删除调试方案");
      return nextState;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`删除调试方案失败: ${message}`);
      return null;
    } finally {
      setActionBusy(false);
    }
  }, [apiPort, onStatusChange, publishStateChange, sessionId, workspaceId]);

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
