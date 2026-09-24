import { useCallback, useRef } from "react";

import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  getGatewayUserViewState,
  putGatewayUserViewState,
} from "../../api/gateway/userViewState";
import type {
  GatewayUserAccess,
  GatewayUserViewState,
} from "../../types/backend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { cloneMaps } from "../../state/appStateMaps";
import { errorMessage } from "../../utils/errorMessage";
import type { SetAppState } from "../contentViewLoaderTypes";
import { canAcceptUserViewStateMutation } from "../workspace/useWorkspaceBootstrap";
import { trackInFlightRequest } from "../runtime/inFlightRequests";

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
  ) => Promise<SessionViewStateLoadOutcome>;
  saveSessionViewState: (payload: SessionViewStatePayload) => void;
  toggleExpandDetails: (expand: boolean) => void;
}

/** 视图状态读取结果：区分「后端权威地没有保存视图」（loaded + null）、「读取失败」
 * （failed + 原始错误）和「当前没有可读取的目标」（skipped）。以前失败与权威空都
 * 返回 null，调用方无法分辨。 */
export type SessionViewStateLoadOutcome =
  | { kind: "loaded"; viewState: GatewayUserViewState | null }
  | { kind: "failed"; error: unknown }
  | { kind: "skipped" };

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
  const requestsRef = useRef(new Map<string, Promise<SessionViewStateLoadOutcome>>());
  // 每条视图状态的 lease 代际登记表：gatewayUserViewStates 只按会话 scope 存值，
  // 本身不带代际信息，无法判断某条残留是否来自上一代 lease。这里记录「这条本地
  // 值是在哪一代 lease 下取得的」，让所有应用路径能共用同一条代际判据。
  const scopeLeaseGenerationRef = useRef(new Map<string, number>());

  // 视图状态落库的唯一出口：按会话 scope 缓存后端权威对象，并只在当前会话
  // 命中时同步工具详情展开态。
  const applyViewState = useCallback((
    workspaceId: string,
    sessionId: string,
    viewState: GatewayUserViewState | null,
    leaseGeneration: number,
    toolDetailsExpanded?: boolean,
  ) => {
    setState((previous) => {
      const next = cloneMaps(previous);
      const cacheKey = sessionScopeKey(workspaceId, sessionId);
      if (viewState) {
        next.gatewayUserViewStates.set(cacheKey, viewState);
        scopeLeaseGenerationRef.current.set(cacheKey, leaseGeneration);
      } else {
        next.gatewayUserViewStates.delete(cacheKey);
        scopeLeaseGenerationRef.current.delete(cacheKey);
      }
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
    requestLeaseGeneration: number,
    // 是否用该对象同步工具详情展开态：只有读取路径需要；保存路径的展开态由用户
    // 这次操作本身决定，不能被响应里的旧对象反推覆盖。
    syncToolDetailsExpanded: boolean,
  ) => {
    const current = hostRef.current;
    // 视图状态应用的唯一前置：当前访问必须是同一用户的同一 lease 代际。读取命中
    // 本地缓存、读到后端响应、保存响应落库这三条路径都先过这里，不再各自判一遍。
    if (current.gatewayUserAccess?.kind !== "user") return;
    if (current.gatewayUserAccess.user_id !== expectedUserId) return;
    if (!canAcceptUserViewStateMutation({
      currentUserId: current.gatewayUserAccess.user_id,
      responseUserId: viewState?.user_id ?? expectedUserId,
      currentLeaseGeneration: current.gatewayUserAccess.lease_generation,
      requestLeaseGeneration,
    })) {
      return;
    }
    applyViewState(
      workspaceId,
      sessionId,
      viewState,
      requestLeaseGeneration,
      syncToolDetailsExpanded ? viewState?.tool_details_expanded ?? false : undefined,
    );
  }, [applyViewState]);

  const loadSessionViewState = useCallback(
    async (workspaceId: string | null, sessionId: string): Promise<SessionViewStateLoadOutcome> => {
      const current = hostRef.current;
      if (!workspaceId || current.gatewayUserAccess?.kind !== "user") {
        return { kind: "skipped" };
      }
      const userId = current.gatewayUserAccess.user_id;
      if (!userId) return { kind: "skipped" };
      const leaseGeneration = current.gatewayUserAccess.lease_generation;
      const cacheKey = sessionScopeKey(workspaceId, sessionId);
      const requestKey = [
        current.apiPort ?? DEFAULT_BACKEND_PORT,
        userId,
        leaseGeneration,
        cacheKey,
      ].join(":");
      // 本地残留必须属于当前 lease 才能命中：接管后 user_id 不变而代际换代时，
      // 上一代的残留既不能应用，也不能当作缓存短路掉后端读取。
      const existingLeaseGeneration = scopeLeaseGenerationRef.current.get(cacheKey);
      const existingStateBelongsToCurrentLease = existingLeaseGeneration === undefined
        || existingLeaseGeneration === leaseGeneration;
      const existingState = current.gatewayUserViewStates.get(cacheKey);
      if (existingState && existingStateBelongsToCurrentLease) {
        writeSessionViewStateCache(cacheRef.current, requestKey, existingState);
        applyLoadedViewState(
          existingState,
          workspaceId,
          sessionId,
          userId,
          leaseGeneration,
          true,
        );
        return { kind: "loaded", viewState: existingState };
      }
      const cached = cacheRef.current.get(requestKey);
      if (cacheRef.current.has(requestKey)) {
        applyLoadedViewState(
          cached ?? null,
          workspaceId,
          sessionId,
          userId,
          leaseGeneration,
          true,
        );
        return { kind: "loaded", viewState: cached ?? null };
      }
      const existingRequest = requestsRef.current.get(requestKey);
      if (existingRequest) return await existingRequest;

      const requestLeaseGeneration = current.gatewayUserAccess.lease_generation;
      const request: Promise<SessionViewStateLoadOutcome> = getGatewayUserViewState(
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
          true,
        );
        return { kind: "loaded", viewState } as const;
      }, (error: unknown) => {
        if (hostRef.current.gatewayUserAccess?.lease_generation === requestLeaseGeneration) {
          setStatus(`读取用户视图位置失败: ${errorMessage(error)}`);
        }
        return { kind: "failed", error } as const;
      });
      trackInFlightRequest(requestsRef.current, requestKey, request);
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
      // 保存响应同样走唯一应用前置：代际/用户不匹配时丢弃，绝不写进当前用户状态。
      applyLoadedViewState(
        updated,
        workspaceId,
        sessionId,
        updated.user_id,
        requestLeaseGeneration,
        false,
      );
    }).catch((error: unknown) => {
      const latest = hostRef.current;
      if (latest.gatewayUserAccess?.lease_generation !== requestLeaseGeneration) return;
      let message = `保存用户视图位置失败: ${errorMessage(error)}`;
      // 保存失败时本地镜像可能与后端真值不一致（toggleExpandDetails 更是乐观置位后
      // 才发现保存失败）。按前端状态管理原则主动重取校准，让本地回到后端权威值，
      // 而不是把错误留在本地继续误导后续投影。
      void getGatewayUserViewState(
        latest.apiPort ?? DEFAULT_BACKEND_PORT,
        workspaceId,
        sessionId,
      ).then((viewState) => {
        writeSessionViewStateCache(cacheRef.current, [
          latest.apiPort ?? DEFAULT_BACKEND_PORT,
          latest.gatewayUserAccess?.user_id ?? "",
          requestLeaseGeneration,
          cacheKey,
        ].join(":"), viewState);
        applyLoadedViewState(
          viewState,
          workspaceId,
          sessionId,
          latest.gatewayUserAccess?.user_id ?? "",
          requestLeaseGeneration,
          true,
        );
        if (hostRef.current.gatewayUserAccess?.lease_generation === requestLeaseGeneration) {
          setStatus(message);
        }
      }, (recalibrationError: unknown) => {
        message = `${message}；重新读取用户视图位置也失败: ${errorMessage(recalibrationError)}`;
        if (hostRef.current.gatewayUserAccess?.lease_generation === requestLeaseGeneration) {
          setStatus(message);
        }
      });
    });
  }, [applyLoadedViewState, setStatus]);

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
