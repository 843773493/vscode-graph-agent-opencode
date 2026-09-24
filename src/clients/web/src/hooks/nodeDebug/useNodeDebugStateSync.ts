import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { errorMessage } from "../../utils/errorMessage";

import {
  getNodeDebugCapabilities,
  getNodeDebugState,
} from "../../api";
import type {
  NodeDebugCapabilities,
  NodeDebugState,
} from "../../types/backend";
import {
  NodeDebugMutationGate,
  type NodeDebugMutation,
} from "./nodeDebugMutationGate";
import { createNodeDebugSyncChannel } from "./nodeDebugSync";
import { trackInFlightRequest } from "../runtime/inFlightRequests";

interface UseNodeDebugStateSyncOptions {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
  ownerKey: string;
  enabled: boolean;
  mutationGate: NodeDebugMutationGate;
}

export interface NodeDebugStateSync {
  state: NodeDebugState | null;
  capabilities: NodeDebugCapabilities | null;
  error: string | null;
  loading: boolean;
  actionBusy: boolean;
  setState: Dispatch<SetStateAction<NodeDebugState | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
  refresh: (force?: boolean) => Promise<void>;
  refreshAfterMutationFailure: (
    message: string,
    mutation: NodeDebugMutation,
  ) => Promise<void>;
  publishStateChange: () => void;
  syncMutationFlags: () => void;
}

export function useNodeDebugStateSync({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  ownerKey,
  enabled,
  mutationGate,
}: UseNodeDebugStateSyncOptions): NodeDebugStateSync {
  const [state, setState] = useState<NodeDebugState | null>(null);
  const [capabilities, setCapabilities] = useState<NodeDebugCapabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const pollGenerationRef = useRef(0);
  const syncChannelRef = useRef<ReturnType<typeof createNodeDebugSyncChannel> | null>(null);
  const stateRequestsRef = useRef<Map<string, Promise<NodeDebugState>>>(new Map());
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const syncMutationFlags = useCallback(() => {
    const busyFlags = mutationGate.busyFlags(enabledRef.current);
    setActionBusy(busyFlags.actionBusy);
    setLoading(busyFlags.loading);
  }, [mutationGate]);

  useLayoutEffect(() => {
    if (mutationGate.switchOwner(ownerKey)) {
      setState(null);
      setCapabilities(null);
      setError(null);
    }
    syncMutationFlags();
  }, [enabled, ownerKey, mutationGate, syncMutationFlags]);

  const loadState = useCallback((force = false): Promise<NodeDebugState> => {
    if (!sessionId) {
      return Promise.reject(new Error("当前没有可读取调试状态的会话"));
    }
    const requestKey = `${apiPort}:${workspaceId ?? ""}:${sessionId}:${threadId}`;
    if (!force) {
      const inFlight = stateRequestsRef.current.get(requestKey);
      if (inFlight) return inFlight;
    }
    const request = getNodeDebugState(apiPort, sessionId, threadId, workspaceId);
    trackInFlightRequest(stateRequestsRef.current, requestKey, request);
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
    if (mutationGate.isCurrentSnapshot(refreshSnapshot, true)) {
      setState(nextState);
    }
  }, [loadState, mutationGate, ownerKey, sessionId]);

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
    mutation: NodeDebugMutation,
  ) => {
    try {
      const authoritativeState = await loadState(true);
      if (mutationGate.isCurrentMutation(mutation)) {
        setState(authoritativeState);
      }
    } catch (refreshCause: unknown) {
      const refreshMessage = errorMessage(refreshCause);
      if (mutationGate.isCurrentMutation(mutation)) {
        setError(`${message}；重新获取调试状态失败: ${refreshMessage}`);
      }
    }
  }, [loadState, mutationGate]);

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
      const pollSnapshot = mutationGate.captureSnapshot(effectOwnerKey);
      try {
        const [nextState, nextCapabilities] = await Promise.all([
          sessionId ? loadState() : Promise.resolve(null),
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
          setError(errorMessage(cause));
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => {
      if (!sessionId) return;
      const pollSnapshot = mutationGate.captureSnapshot(effectOwnerKey);
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
            setError(errorMessage(cause));
          }
        });
    }, 800);

    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [apiPort, enabled, loadState, mutationGate, ownerKey, sessionId, threadId, syncMutationFlags, workspaceId]);

  return {
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
  };
}
