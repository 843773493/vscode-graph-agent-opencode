import { useCallback, useRef } from "react";

import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  getGatewayUserViewState,
  putGatewayUserViewState,
} from "../../gatewayApi";
import type {
  GatewayUserAccess,
  GatewayUserViewState,
} from "../../types/backend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { cloneMaps } from "../../state/appStateMaps";
import type { SetAppState } from "../contentViewLoaderTypes";
import { canAcceptUserViewStateMutation } from "../workspace/useWorkspaceBootstrap";

const SESSION_VIEW_STATE_CACHE_LIMIT = 64;

export interface SessionViewStatePayload {
  turn_anchor: string | null;
  scroll_offset: number;
  follow_latest: boolean;
  tool_details_expanded?: boolean;
}

export interface SessionViewStateHost {
  apiPort: number | null;
  currentWorkspaceId: string | null;
  currentSessionId: string | null;
  gatewayUserAccess: GatewayUserAccess | null;
  gatewayUserViewStates: Map<string, GatewayUserViewState>;
  expandDetails: boolean;
}

interface UseSessionViewStateOptions {
  host: SessionViewStateHost;
  setState: SetAppState;
  setStatus: (message: string) => void;
}

export interface SessionViewStateController {
  loadSessionViewState: (
    workspaceId: string | null,
    sessionId: string,
  ) => Promise<GatewayUserViewState | null | undefined>;
  saveSessionViewState: (payload: SessionViewStatePayload) => void;
  toggleExpandDetails: (expand: boolean) => void;
}

function writeSessionViewStateCache(
  cache: Map<string, GatewayUserViewState | null>,
  key: string,
  value: GatewayUserViewState | null,
): void {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > SESSION_VIEW_STATE_CACHE_LIMIT) {
    const oldestKey = cache.keys().next().value;
    if (typeof oldestKey !== "string") break;
    cache.delete(oldestKey);
  }
}

export function useSessionViewState({
  host,
  setState,
  setStatus,
}: UseSessionViewStateOptions): SessionViewStateController {
  const hostRef = useRef(host);
  hostRef.current = host;
  const cacheRef = useRef(new Map<string, GatewayUserViewState | null>());
  const requestsRef = useRef(new Map<string, Promise<GatewayUserViewState | null>>());

  // 视图状态落库的唯一出口：按会话 scope 缓存后端权威对象，并只在当前会话
  // 命中时同步工具详情展开态。
  const applyViewState = useCallback((
    workspaceId: string,
    sessionId: string,
    viewState: GatewayUserViewState | null,
    toolDetailsExpanded?: boolean,
  ) => {
    setState((previous) => {
      const next = cloneMaps(previous);
      const cacheKey = sessionScopeKey(workspaceId, sessionId);
      if (viewState) next.gatewayUserViewStates.set(cacheKey, viewState);
      else next.gatewayUserViewStates.delete(cacheKey);
      if (
        toolDetailsExpanded !== undefined
        && previous.currentSession?.session_id === sessionId
        && previous.currentSessionWorkspaceId === workspaceId
      ) {
        next.expandDetails = toolDetailsExpanded;
      }
      return next;
    });
  }, [setState]);

  const applyLoadedViewState = useCallback((
    viewState: GatewayUserViewState | null,
    workspaceId: string,
    sessionId: string,
    expectedUserId: string,
    expectedLeaseGeneration: number,
  ) => {
    const current = hostRef.current;
    if (
      !canAcceptUserViewStateMutation({
        currentUserId: current.gatewayUserAccess?.user_id,
        responseUserId: viewState?.user_id ?? expectedUserId,
        currentLeaseGeneration: current.gatewayUserAccess?.lease_generation,
        requestLeaseGeneration: expectedLeaseGeneration,
      })
      || current.gatewayUserAccess?.user_id !== expectedUserId
    ) {
      return;
    }
    applyViewState(
      workspaceId,
      sessionId,
      viewState,
      viewState?.tool_details_expanded ?? false,
    );
  }, [applyViewState]);

  const loadSessionViewState = useCallback(
    async (workspaceId: string | null, sessionId: string) => {
      const current = hostRef.current;
      if (!workspaceId || current.gatewayUserAccess?.kind !== "user") return;
      const userId = current.gatewayUserAccess.user_id;
      if (!userId) return;
      const leaseGeneration = current.gatewayUserAccess.lease_generation;
      const cacheKey = sessionScopeKey(workspaceId, sessionId);
      const requestKey = [
        current.apiPort ?? DEFAULT_BACKEND_PORT,
        userId,
        leaseGeneration,
        cacheKey,
      ].join(":");
      const existingState = current.gatewayUserViewStates.get(cacheKey);
      if (existingState) {
        writeSessionViewStateCache(cacheRef.current, requestKey, existingState);
        applyLoadedViewState(
          existingState,
          workspaceId,
          sessionId,
          userId,
          leaseGeneration,
        );
        return existingState;
      }
      const cached = cacheRef.current.get(requestKey);
      if (cacheRef.current.has(requestKey)) {
        applyLoadedViewState(
          cached ?? null,
          workspaceId,
          sessionId,
          userId,
          leaseGeneration,
        );
        return cached ?? null;
      }
      const existingRequest = requestsRef.current.get(requestKey);
      if (existingRequest) return await existingRequest;

      const requestLeaseGeneration = current.gatewayUserAccess.lease_generation;
      const request = getGatewayUserViewState(
        current.apiPort ?? DEFAULT_BACKEND_PORT,
        workspaceId,
        sessionId,
      ).then((viewState) => {
        writeSessionViewStateCache(cacheRef.current, requestKey, viewState);
        applyLoadedViewState(
          viewState,
          workspaceId,
          sessionId,
          userId,
          leaseGeneration,
        );
        return viewState;
      }, (error: unknown) => {
        if (hostRef.current.gatewayUserAccess?.lease_generation === requestLeaseGeneration) {
          setStatus(`读取用户视图位置失败: ${error instanceof Error ? error.message : String(error)}`);
        }
        return null;
      });
      requestsRef.current.set(requestKey, request);
      void request.then(() => {
        if (requestsRef.current.get(requestKey) === request) {
          requestsRef.current.delete(requestKey);
        }
      });
      return await request;
    }, [applyLoadedViewState, setStatus],
  );

  const saveSessionViewState = useCallback((payload: SessionViewStatePayload) => {
    const current = hostRef.current;
    const workspaceId = current.currentWorkspaceId;
    const sessionId = current.currentSessionId;
    if (!workspaceId || !sessionId || current.gatewayUserAccess?.kind !== "user") return;
    const requestLeaseGeneration = current.gatewayUserAccess.lease_generation;
    const cacheKey = sessionScopeKey(workspaceId, sessionId);
    const existing = current.gatewayUserViewStates.get(cacheKey);
    void putGatewayUserViewState(
      current.apiPort ?? DEFAULT_BACKEND_PORT,
      workspaceId,
      sessionId,
      {
        turn_anchor: payload.turn_anchor,
        scroll_offset: payload.scroll_offset,
        follow_latest: payload.follow_latest,
        projection_version: existing?.projection_version ?? 1,
        tool_details_expanded: payload.tool_details_expanded
          ?? existing?.tool_details_expanded
          ?? current.expandDetails,
      },
    ).then((updated) => {
      writeSessionViewStateCache(
        cacheRef.current,
        [
          current.apiPort ?? DEFAULT_BACKEND_PORT,
          updated.user_id,
          requestLeaseGeneration,
          cacheKey,
        ].join(":"),
        updated,
      );
      const latest = hostRef.current;
      if (!canAcceptUserViewStateMutation({
        currentUserId: latest.gatewayUserAccess?.user_id,
        responseUserId: updated.user_id,
        currentLeaseGeneration: latest.gatewayUserAccess?.lease_generation,
        requestLeaseGeneration,
      })) {
        return;
      }
      applyViewState(workspaceId, sessionId, updated);
    }).catch((error: unknown) => {
      if (hostRef.current.gatewayUserAccess?.lease_generation === requestLeaseGeneration) {
        setStatus(`保存用户视图位置失败: ${error instanceof Error ? error.message : String(error)}`);
      }
    });
  }, [applyViewState, setStatus]);

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState((previous) => ({ ...previous, expandDetails: expand }));
    const current = hostRef.current;
    const existing = current.currentWorkspaceId && current.currentSessionId
      ? current.gatewayUserViewStates.get(
          sessionScopeKey(current.currentWorkspaceId, current.currentSessionId),
        )
      : null;
    saveSessionViewState({
      turn_anchor: existing?.turn_anchor ?? null,
      scroll_offset: existing?.scroll_offset ?? 0,
      follow_latest: existing?.follow_latest ?? true,
      tool_details_expanded: expand,
    });
  }, [saveSessionViewState, setState]);

  return {
    loadSessionViewState,
    saveSessionViewState,
    toggleExpandDetails,
  };
}
