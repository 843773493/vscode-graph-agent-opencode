import { useCallback, useEffect, useRef } from "react";
import { errorMessage } from "../../utils/errorMessage";

import {
  clearPendingRequests as apiClearPendingRequests,
  reorderPendingRequests as apiReorderPendingRequests,
  listThreadPendingRequests as apiListThreadPendingRequests,
  removePendingRequest as apiRemovePendingRequest,
  updatePendingRequestPolicy as apiUpdatePendingRequestPolicy,
} from "../../pendingRequestsApi";
import { cloneMaps } from "../../state/appStateMaps";
import {
  writePendingSnapshot,
} from "../../state/conversations";
import type {
  DeliveryPolicy,
  PendingRequestList,
  Session,
} from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";


export function usePendingRequestActions({
  apiPort,
  currentSession,
  currentThreadId,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  currentThreadId?: string;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  setState: SetAppState;
}) {
  const orderRequest = useRef<AbortController | null>(null);
  useEffect(() => () => orderRequest.current?.abort(), [currentSessionCacheKey, currentThreadId]);
  const requireTarget = useCallback(() => {
    if (!currentSession) {
      throw new Error("当前没有可操作的会话");
    }
    return {
      sessionId: currentSession.session_id,
      threadId: currentThreadId,
      workspaceId: currentSessionGatewayWorkspaceId,
      cacheKey: currentSessionCacheKey ?? currentSession.session_id,
    };
  }, [
    currentSession,
    currentThreadId,
    currentSessionCacheKey,
    currentSessionGatewayWorkspaceId,
  ]);

  const replaceSnapshot = useCallback((
    sessionId: string,
    sessionCacheKey: string,
    snapshot: PendingRequestList,
  ) => {
    if (snapshot.session_id !== sessionId) {
      throw new Error("待处理快照与请求的 Session 归属不一致");
    }
    setState((previous) => {
      const next = cloneMaps(previous);
      writePendingSnapshot(
        next.pendingConversations,
        next.activeJobIdsBySession,
        snapshot,
        sessionCacheKey,
        next.pendingQueueVersions,
      );
      return next;
    });
  }, [setState]);

  const refreshSnapshot = useCallback(async (
    sessionId: string,
    workspaceId: string | null,
    cacheKey: string,
    threadId?: string,
  ) => {
    replaceSnapshot(
      sessionId,
      cacheKey,
      await apiListThreadPendingRequests(apiPort, sessionId, workspaceId, undefined, threadId),
    );
  }, [apiPort, replaceSnapshot]);

  const applyServerMutation = useCallback(async (
    target: {
      sessionId: string;
      workspaceId: string | null;
      cacheKey: string;
      threadId?: string;
    },
    request: () => Promise<PendingRequestList>,
  ) => {
    try {
      await request();
      // 变更后重读发起实例的 Thread 快照，不把 Session 聚合写入子线程。
      await refreshSnapshot(target.sessionId, target.workspaceId, target.cacheKey, target.threadId);
    } catch (error) {
      // 补偿重取失败不得覆盖原始错误：变更请求返回的 409/422 才是用户真正
      // 需要看到的失败原因，对账用的待处理队列重取失败只是次生信息。
      try {
        await refreshSnapshot(
          target.sessionId,
          target.workspaceId,
          target.cacheKey,
          target.threadId,
        );
      } catch (recoveryError) {
        const recoveryMessage = errorMessage(recoveryError);
        // 必须是同一个错误对象：上层按 HttpRequestError 与 status 判断语义。
        if (error instanceof Error) {
          error.message =
            `${error.message}；重新读取待处理队列也失败: ${recoveryMessage}`;
        }
      }
      throw error;
    }
  }, [refreshSnapshot]);

  const removePendingRequest = useCallback(async (messageId: string) => {
    const target = requireTarget();
    await applyServerMutation(target, () =>
      apiRemovePendingRequest(
        apiPort,
        target.sessionId,
        messageId,
        target.workspaceId,
      ),
    );
  }, [apiPort, applyServerMutation, requireTarget]);

  const clearPendingRequests = useCallback(async () => {
    const target = requireTarget();
    await applyServerMutation(target, () =>
      apiClearPendingRequests(
        apiPort,
        target.sessionId,
        target.workspaceId,
        target.threadId,
      ),
    );
  }, [apiPort, applyServerMutation, requireTarget]);

  const updatePendingRequestPolicy = useCallback(async (
    messageId: string,
    deliveryPolicy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => {
    const target = requireTarget();
    await applyServerMutation(target, () =>
      apiUpdatePendingRequestPolicy(
        apiPort,
        target.sessionId,
        messageId,
        {
          delivery_policy: deliveryPolicy,
          expected_snapshot_version: expectedSnapshotVersion,
        },
        target.workspaceId,
      ),
    );
  }, [apiPort, applyServerMutation, requireTarget]);

  const reorderPendingRequests = useCallback(async (messageIds: string[], expectedRevision: number) => {
    const target = requireTarget();
    if (!target.threadId) throw new Error("重排必须指定 Thread");
    orderRequest.current?.abort();
    const controller = new AbortController();
    orderRequest.current = controller;
    let snapshot: PendingRequestList;
    try {
      snapshot = await apiReorderPendingRequests(apiPort, target.sessionId, target.threadId, messageIds,
        expectedRevision, target.workspaceId, controller.signal);
    } catch (error) {
      if (controller.signal.aborted) return;
      // 409、超时或丢失回执均重读权威顺序；不猜测服务端是否提交成功。
      snapshot = await apiListThreadPendingRequests(apiPort, target.sessionId, target.workspaceId, controller.signal, target.threadId);
    }
    if (!controller.signal.aborted) replaceSnapshot(target.sessionId, target.cacheKey, snapshot);
  }, [apiPort, replaceSnapshot, requireTarget]);

  return {
    reorderPendingRequests,
    removePendingRequest,
    clearPendingRequests,
    updatePendingRequestPolicy,
  };
}
