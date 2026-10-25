import { useCallback } from "react";
import {
  getSession as apiGetSession,
  getSessionCatalogBreadcrumb,
  listSessionCatalogChildren,
} from "../../../api";
import { replaceSessionMetadata } from "../../../state/session/sessions";
import { appendFrontendEvent } from "../../../state/traceEvents";
import { sessionScopeKey } from "../../../state/session/sessionScope";
import { errorMessage } from "../../../utils/errorMessage";
import { requireGatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import {
  catalogOutboxOperationIsTerminal,
  type CatalogOutboxIntent,
  type CatalogOutboxOperation,
} from "../../../state/session/sessionCatalogOutbox";
import { invalidateWorkspaceSessionListSnapshot } from "../../workspace/workspaceSessionListRefresh";
import type { SessionLifecycleContext } from "./sessionLifecycleContext";
import {
  collectCascadedSessionIds,
  convergeSessionStateAfterRemoval,
  currentSessionMatchesOwner,
  normalizeSessionTitle,
} from "./sessionLifecycleTransitions";

export function useSessionCatalogActions(context: SessionLifecycleContext) {
  const {
    apiPort, currentSession, activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId, setState, sessionCatalogOutboxRuntime,
    gatewayUserAccess, workspaceRequestTargets,
  } = context;

  const workspaceRequestTarget = useCallback(
    (workspaceId: string) => requireGatewayWorkspaceRequestTarget(workspaceRequestTargets, workspaceId),
    [workspaceRequestTargets],
  );

  const submitCatalogMutation = useCallback(async (
    workspaceId: string,
    intent: CatalogOutboxIntent,
    baseCatalogRevision: number,
    expectedRevision?: number,
  ) => {
    if (!sessionCatalogOutboxRuntime || !gatewayUserAccess) {
      throw new Error("Gateway 认证状态尚未就绪，不能修改会话目录");
    }
    const { handle, operationId } = await sessionCatalogOutboxRuntime.submit(
      workspaceRequestTarget(workspaceId),
      gatewayUserAccess,
      intent,
      {
        baseCatalogRevision,
        ...(expectedRevision === undefined ? {} : { expectedRevision }),
      },
    );
    setState((prev) => ({ ...prev, status: "会话目录操作已保存本地待同步变更，等待提交" }));
    return { handle, operationId };
  }, [gatewayUserAccess, sessionCatalogOutboxRuntime, setState, workspaceRequestTarget]);

  const commitCatalogMutation = useCallback(async (
    workspaceId: string,
    intent: CatalogOutboxIntent,
    baseCatalogRevision: number,
    expectedRevision?: number,
  ) => {
    if (!sessionCatalogOutboxRuntime) {
      throw new Error("Gateway 会话目录 outbox runtime 尚未初始化");
    }
    const submitted = await submitCatalogMutation(
      workspaceId,
      intent,
      baseCatalogRevision,
      expectedRevision,
    );
    const operation = await sessionCatalogOutboxRuntime.waitForTerminal(
      submitted.handle,
      submitted.operationId,
    );
    return { ...submitted, operation };
  }, [sessionCatalogOutboxRuntime, submitCatalogMutation]);

  const renameSession = useCallback(
    async (sessionId: string, title: string, workspaceId?: string | null) => {
      const workspaceIdForRequest =
        workspaceId ?? currentSessionGatewayWorkspaceId;
      const statusOwner = {
        sessionId: currentSession?.session_id ?? null,
        workspaceId: currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId,
      };
      try {
        const normalizedTitle = normalizeSessionTitle(title);
        if (!workspaceIdForRequest) {
          throw new Error("重命名会话缺少 Gateway workspace route ID");
        }
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceIdForRequest),
          sessionId,
        );
        const node = breadcrumb.items.find((item) => item.node_id === sessionId);
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(`会话目录中找不到目标会话节点: ${sessionId}`);
        }
        await commitCatalogMutation(
          workspaceIdForRequest,
          { kind: "rename_node", targetNodeId: node.node_id, name: normalizedTitle },
          breadcrumb.catalog_revision,
          node.node_revision,
        );
        const updatedSession = await apiGetSession(
          apiPort,
          sessionId,
          workspaceIdForRequest,
        );
        setState((prev) => {
          const next = replaceSessionMetadata(
            prev,
            updatedSession,
            workspaceIdForRequest,
          );
          if (currentSessionMatchesOwner(prev, statusOwner)) {
            next.status = `已命名会话: ${updatedSession.title}`;
          }
          const cacheKey = sessionScopeKey(
            workspaceIdForRequest,
            updatedSession.session_id,
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "session_renamed",
            "命名会话",
            {
              session_id: updatedSession.session_id,
              title: updatedSession.title,
            },
            updatedSession.title,
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => currentSessionMatchesOwner(prev, statusOwner)
          ? { ...prev, status: `会话命名失败: ${message}` }
          : prev);
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      commitCatalogMutation,
      currentSession,
      currentSessionGatewayWorkspaceId,
      setState,
      workspaceRequestTarget,
    ],
  );

  const setSessionParent = useCallback(
    async (
      workspaceId: string,
      sessionId: string,
      parentSessionId: string | null,
    ) => {
      setState((prev) => ({
        ...prev,
        status: parentSessionId ? "正在提交子会话关系" : "正在提交父会话解除",
      }));

      try {
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceId),
          sessionId,
        );
        const node = breadcrumb.items.find((item) => item.node_id === sessionId);
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(`会话目录中找不到目标会话节点: ${sessionId}`);
        }
        await commitCatalogMutation(
          workspaceId,
          {
            kind: "move_node",
            targetNodeId: node.node_id,
            parentNodeId: parentSessionId,
          },
          breadcrumb.catalog_revision,
          node.node_revision,
        );
        const updatedSession = await apiGetSession(apiPort, sessionId, workspaceId);
        setState((prev) => {
          const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
          next.status = parentSessionId
            ? `已将「${updatedSession.title}」绑定为子会话`
            : `已解除「${updatedSession.title}」的父会话绑定`;
          return next;
        });
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => ({ ...prev, status: `更新会话树失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, commitCatalogMutation, setState, workspaceRequestTarget],
  );

  const deleteSession = useCallback(
    async (sessionId: string, workspaceId?: string | null) => {
      const workspaceIdForRequest =
        workspaceId ?? currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId;
      let catalogRefreshFailure: unknown = null;
      try {
        if (!workspaceIdForRequest) {
          throw new Error("删除会话缺少 Gateway workspace route ID");
        }
        setState((prev) => ({ ...prev, status: "正在提交会话删除" }));
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceIdForRequest),
          sessionId,
        );
        const nodeIndex = breadcrumb.items.findIndex((item) => item.node_id === sessionId);
        const node = breadcrumb.items[nodeIndex];
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(`会话目录中找不到目标会话节点: ${sessionId}`);
        }
        const parentNodeId = nodeIndex > 0
          ? breadcrumb.items[nodeIndex - 1]!.node_id
          : null;
        if (!sessionCatalogOutboxRuntime) {
          throw new Error("Gateway 会话目录 outbox runtime 尚未初始化");
        }
        const { handle, operationId } = await submitCatalogMutation(
          workspaceIdForRequest,
          { kind: "delete_session", targetNodeId: node.node_id },
          breadcrumb.catalog_revision,
          node.node_revision,
        );
        let terminalError: unknown = null;
        let operation: CatalogOutboxOperation | null = null;
        try {
          operation = await sessionCatalogOutboxRuntime.waitForTerminal(handle, operationId);
        } catch (error: unknown) {
          terminalError = error;
        }
        // 拒绝时 waitForTerminal 保持原有抛错契约；只在该失败路径读取本地终态详情。
        if (operation === null && terminalError !== null) {
          operation = handle.driver.current().operations.find(
            (candidate) => candidate.client_operation_id === operationId,
          ) ?? null;
        }
        if (!operation || !catalogOutboxOperationIsTerminal(operation)) {
          throw terminalError ?? new Error(
            `会话目录删除未产生可核对的终态回执: ${operationId}`,
          );
        }
        try {
          const page = await listSessionCatalogChildren(
            apiPort,
            workspaceRequestTarget(workspaceIdForRequest),
            parentNodeId,
          );
          const committedRevision = operation.committed_catalog_revision;
          if (operation.state === "committed" && committedRevision === null) {
            throw new Error(
              `已提交的会话删除缺少 committed_catalog_revision: ${operation.client_operation_id}`,
            );
          }
          if (!Number.isSafeInteger(page.catalog_revision)
            || (committedRevision !== null && page.catalog_revision < committedRevision)) {
            throw new Error(
              `会话目录分支 revision 尚未追上删除回执 ${operation.client_operation_id}: `
              + `需要至少 ${committedRevision ?? "有效 revision"}，实际 ${page.catalog_revision}`,
            );
          }
          await handle.driver.prune(
            page.catalog_revision,
            [operation.client_operation_id],
          );
        } catch (error) {
          catalogRefreshFailure = error;
          const status = operation.settlement === "repair_required"
            ? `删除已提交但排空失败，需修复: ${operation.error_detail ?? operation.error_code}；目录分支刷新失败`
            : "已到终态但目录分支刷新失败";
          sessionCatalogOutboxRuntime?.reportError(
            `会话目录操作 ${operation.client_operation_id} ${status}: `
            + errorMessage(error),
          );
        }
        if (operation.state !== "committed") {
          throw terminalError ?? new Error(
            `会话目录删除被 ${operation.state}: `
            + `${operation.error_code ?? "unknown"} ${operation.error_detail ?? ""}`.trim(),
          );
        }
        if (operation.settlement !== "settled") {
          throw terminalError ?? new Error(
            `会话删除已提交但排空未完成 (${operation.settlement ?? "缺少结算回执"}): ${operationId}`,
          );
        }
        if (terminalError && catalogRefreshFailure === null) {
          sessionCatalogOutboxRuntime.reportError(
            `会话目录操作 ${operationId} 已提交且分支已刷新，但终态确认异常: `
            + errorMessage(terminalError),
          );
        }
      } catch (error) {
        const failure = catalogRefreshFailure === null
          ? error
          : new AggregateError(
            [error, catalogRefreshFailure],
            `${errorMessage(error)}；目录分支刷新失败: ${errorMessage(catalogRefreshFailure)}`,
          );
        setState((prev) => ({
          ...prev,
          status: `删除会话失败: ${errorMessage(failure)}`,
        }));
        throw failure;
      }

      const resolvedWorkspaceId = workspaceIdForRequest;
      invalidateWorkspaceSessionListSnapshot(apiPort, resolvedWorkspaceId);
      setState((prev) => {
        const previousSessions = prev.sessionsByWorkspace.get(resolvedWorkspaceId)
          ?? prev.sessions;
        const doomed = collectCascadedSessionIds(previousSessions, sessionId);
        const remainingSessions = previousSessions.filter(
          (session) => !doomed.has(session.session_id),
        );
        const next = convergeSessionStateAfterRemoval(
          prev,
          resolvedWorkspaceId,
          remainingSessions,
          { reloadHistoryAfterSwitch: true, selectNextSession: true },
        );

        next.status = catalogRefreshFailure !== null
          ? `已删除会话: ${sessionId}；会话目录分支刷新失败: ${errorMessage(catalogRefreshFailure)}`
          : `已删除会话: ${sessionId}`;
        return next;
      });
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      sessionCatalogOutboxRuntime,
      submitCatalogMutation,
      setState,
      workspaceRequestTarget,
    ],
  );

  return { deleteSession, renameSession, setSessionParent };
}
