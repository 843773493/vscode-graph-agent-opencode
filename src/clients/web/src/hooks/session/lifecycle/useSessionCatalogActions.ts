import { t } from '../../../../../shared/core/i18n/index.js';
import { useCallback } from "react";
import {
  getSession as apiGetSession,
  getSessionCatalogBreadcrumb,
  listSessionCatalogChildren,
} from "../../../api";
import { replaceSessionMetadata } from "../../../state/session/sessions";
import { appendFrontendEvent } from "../../../state/traceEvents";
import { sessionScopeKey } from "../../../state/session/sessionScope";
import { errorMessage, ReportedAggregateError } from "../../../utils/errorMessage";
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
    workspaceRequestTargets,
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
    if (!sessionCatalogOutboxRuntime) {
      throw new Error(t('navigation.catalog.authNotReadyMutate'));
    }
    const { handle, operationId } = await sessionCatalogOutboxRuntime.submit(
      workspaceRequestTarget(workspaceId),
        intent,
      {
        baseCatalogRevision,
        ...(expectedRevision === undefined ? {} : { expectedRevision }),
      },
    );
    setState((prev) => ({ ...prev, status: t('navigation.catalog.savedPending') }));
    return { handle, operationId };
  }, [sessionCatalogOutboxRuntime, setState, workspaceRequestTarget]);

  const commitCatalogMutation = useCallback(async (
    workspaceId: string,
    intent: CatalogOutboxIntent,
    baseCatalogRevision: number,
    expectedRevision?: number,
  ) => {
    if (!sessionCatalogOutboxRuntime) {
      throw new Error(t('navigation.catalog.outboxNotReady'));
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
          throw new Error(t('navigation.sessions.renameRouteMissing'));
        }
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceIdForRequest),
          sessionId,
        );
        const node = breadcrumb.items.find((item) => item.node_id === sessionId);
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(t('navigation.catalog.sessionNodeNotFound', sessionId));
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
            next.status = t('navigation.sessions.named', updatedSession.title);
          }
          const cacheKey = sessionScopeKey(
            workspaceIdForRequest,
            updatedSession.session_id,
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "session_renamed",
            t('navigation.sessions.nameAction'),
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
          ? { ...prev, status: t('navigation.sessions.nameError', message) }
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
        status: parentSessionId ? t('navigation.sessions.bindSubmitting') : t('navigation.sessions.unbindSubmitting'),
      }));

      try {
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceId),
          sessionId,
        );
        const node = breadcrumb.items.find((item) => item.node_id === sessionId);
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(t('navigation.catalog.sessionNodeNotFound', sessionId));
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
            ? t('navigation.sessions.boundTitle', updatedSession.title)
            : t('navigation.sessions.unboundTitle', updatedSession.title);
          return next;
        });
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => ({ ...prev, status: t('navigation.sessions.treeUpdateError', message) }));
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
          throw new Error(t('navigation.sessions.deleteRouteMissing'));
        }
        setState((prev) => ({ ...prev, status: t('navigation.sessions.deleteSubmitting') }));
        const breadcrumb = await getSessionCatalogBreadcrumb(
          apiPort,
          workspaceRequestTarget(workspaceIdForRequest),
          sessionId,
        );
        const nodeIndex = breadcrumb.items.findIndex((item) => item.node_id === sessionId);
        const node = breadcrumb.items[nodeIndex];
        if (!node || node.kind !== "session" || node.session_id !== sessionId) {
          throw new Error(t('navigation.catalog.sessionNodeNotFound', sessionId));
        }
        const parentNodeId = nodeIndex > 0
          ? breadcrumb.items[nodeIndex - 1]!.node_id
          : null;
        if (!sessionCatalogOutboxRuntime) {
          throw new Error(t('navigation.catalog.outboxNotReady'));
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
            t('navigation.catalog.deleteReceiptMissing', operationId),
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
              t('navigation.catalog.deleteCommittedRevisionMissing', operation.client_operation_id),
            );
          }
          if (!Number.isSafeInteger(page.catalog_revision)
            || (committedRevision !== null && page.catalog_revision < committedRevision)) {
            throw new Error(
              t('navigation.catalog.deleteBranchBehind', operation.client_operation_id, committedRevision ?? t('navigation.catalog.validRevision'), page.catalog_revision),
            );
          }
          await handle.driver.prune(
            page.catalog_revision,
            [operation.client_operation_id],
          );
        } catch (error) {
          catalogRefreshFailure = error;
        }
        if (operation.state !== "committed") {
          throw terminalError ?? new Error(
            t('navigation.catalog.deleteRejected', operation.state, `${operation.error_code ?? "unknown"} ${operation.error_detail ?? ""}`.trim()),
          );
        }
        if (operation.settlement !== "settled") {
          throw terminalError ?? new Error(
            t('navigation.catalog.deleteSettlementIncomplete', operation.settlement ?? t('navigation.catalog.settlementReceiptMissing'), operationId),
          );
        }
        if (terminalError && catalogRefreshFailure === null) {
          sessionCatalogOutboxRuntime.reportError(
            t('navigation.catalog.deleteConfirmationError', operationId, errorMessage(terminalError)),
          );
        }
      } catch (error) {
        const failure = catalogRefreshFailure === null
          ? error
          : new ReportedAggregateError(
            [error, catalogRefreshFailure],
            t('navigation.catalog.alsoRefreshError', errorMessage(error), errorMessage(catalogRefreshFailure)),
          );
        if (catalogRefreshFailure !== null) {
          sessionCatalogOutboxRuntime?.reportError(
            t('navigation.sessions.deleteOwnerError', workspaceIdForRequest, sessionId, errorMessage(failure)),
          );
        }
        setState((prev) => ({
          ...prev,
          status: t('navigation.sessions.deleteError', errorMessage(failure)),
        }));
        throw failure;
      }

      if (catalogRefreshFailure !== null) {
        sessionCatalogOutboxRuntime?.reportError(
          t('navigation.sessions.deletedRefreshError', workspaceIdForRequest, sessionId, errorMessage(catalogRefreshFailure)),
        );
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
          ? t('navigation.sessions.deletedRefreshStatus', sessionId, errorMessage(catalogRefreshFailure))
          : t('navigation.sessions.deleted', sessionId);
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
