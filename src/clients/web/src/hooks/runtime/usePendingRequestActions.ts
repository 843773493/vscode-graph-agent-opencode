import { useCallback } from "react";

import {
  clearPendingRequests as apiClearPendingRequests,
  listPendingRequests as apiListPendingRequests,
  removePendingRequest as apiRemovePendingRequest,
  updatePendingRequestPolicy as apiUpdatePendingRequestPolicy,
  updatePendingRequest as apiUpdatePendingRequest,
} from "../../pendingRequestsApi";
import { cloneMaps } from "../../state/appStateMaps";
import {
  writePendingSnapshot,
} from "../../state/conversations";
import type {
  AttachmentRef,
  DeliveryPolicy,
  PendingRequestList,
  Session,
} from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";


export function usePendingRequestActions({
  apiPort,
  currentSession,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  setState: SetAppState;
}) {
  const requireTarget = useCallback(() => {
    if (!currentSession) {
      throw new Error("当前没有可操作的会话");
    }
    return {
      sessionId: currentSession.session_id,
      workspaceId: currentSessionGatewayWorkspaceId,
      cacheKey: currentSessionCacheKey ?? currentSession.session_id,
    };
  }, [
    currentSession,
    currentSessionCacheKey,
    currentSessionGatewayWorkspaceId,
  ]);

  const replaceSnapshot = useCallback((
    sessionId: string,
    sessionCacheKey: string,
    snapshot: PendingRequestList,
  ) => {
    setState((previous) => {
      const next = cloneMaps(previous);
      writePendingSnapshot(
        next.pendingConversations,
        next.activeJobIdsBySession,
        snapshot,
        sessionCacheKey,
      );
      return next;
    });
  }, [setState]);

  const recoverSnapshot = useCallback(async (
    sessionId: string,
    workspaceId: string | null,
    cacheKey: string,
  ) => {
    replaceSnapshot(
      sessionId,
      cacheKey,
      await apiListPendingRequests(apiPort, sessionId, workspaceId),
    );
  }, [apiPort, replaceSnapshot]);

  const applyServerMutation = useCallback(async (
    target: {
      sessionId: string;
      workspaceId: string | null;
      cacheKey: string;
    },
    request: () => Promise<PendingRequestList>,
  ) => {
    try {
      replaceSnapshot(target.sessionId, target.cacheKey, await request());
    } catch (error) {
      // 补偿重取失败不得覆盖原始错误：变更请求返回的 409/422 才是用户真正
      // 需要看到的失败原因，对账用的待处理队列重取失败只是次生信息。
      try {
        await recoverSnapshot(
          target.sessionId,
          target.workspaceId,
          target.cacheKey,
        );
      } catch (recoveryError) {
        const recoveryMessage = recoveryError instanceof Error
          ? recoveryError.message
          : String(recoveryError);
        // 必须是同一个错误对象：上层按 HttpRequestError 与 status 判断语义。
        if (error instanceof Error) {
          error.message =
            `${error.message}；重新读取待处理队列也失败: ${recoveryMessage}`;
        }
      }
      throw error;
    }
  }, [recoverSnapshot, replaceSnapshot]);

  const updatePendingRequest = useCallback(async (
    messageId: string,
    content: string,
    attachments: AttachmentRef[] = [],
  ) => {
    const target = requireTarget();
    await applyServerMutation(target, () =>
      apiUpdatePendingRequest(
        apiPort,
        target.sessionId,
        messageId,
        { content, attachments },
        target.workspaceId,
      ),
    );
  }, [apiPort, applyServerMutation, requireTarget]);

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

  return {
    updatePendingRequest,
    removePendingRequest,
    clearPendingRequests,
    updatePendingRequestPolicy,
  };
}
