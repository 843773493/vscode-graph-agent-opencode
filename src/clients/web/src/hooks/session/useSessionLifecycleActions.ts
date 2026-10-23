import { useCallback, useEffect, type MutableRefObject } from "react";
import { getGatewayHealth } from "../../gatewayApi";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  forkSessionContext as apiForkSessionContext,
  getSession as apiGetSession,
  getSessionCatalogBreadcrumb,
  listAgents as apiListAgents,
  listSessionCatalogChildren,
  listSessions as apiListSessions,
  setWorkspaceDefaultAgent as apiSetWorkspaceDefaultAgent,
  setWorkspaceDefaultProvider as apiSetWorkspaceDefaultProvider,
  updateSessionAgent as apiUpdateSessionAgent,
  updateSessionProvider as apiUpdateSessionProvider,
} from "../../api";
import type { Session, SessionForkRequest } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { cloneMaps } from "../../state/appStateMaps";
import {
  clearLastSessionId,
  writeLastSessionId,
} from "../../state/storage";
import { applySessionListConvergence, replaceSessionMetadata } from "../../state/session/sessions";
import { appendFrontendEvent } from "../../state/traceEvents";
import { resetAgentStateFields } from "../runtime/useAgentStateSnapshot";
import { resetSessionScopedFields } from "./sessionScopedState";
import type { SetAppState } from "../contentViewLoaderTypes";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { errorMessage } from "../../utils/errorMessage";
import { HttpRequestError } from "../../api/http";
import {
  requireGatewayWorkspaceRequestTarget,
  requireForkTargetWorkspace,
  type GatewayWorkspaceForkRoute,
  type GatewayWorkspaceRequestTarget,
} from "../../api/workspaceIdentity";
import type { GatewayUserAccess } from "../../types/backend";
import {
  catalogOutboxOperationIsTerminal,
  type CatalogOutboxIntent,
  type CatalogOutboxOperation,
} from "../../state/session/sessionCatalogOutbox";
import type { SessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";
import { invalidateWorkspaceSessionListSnapshot } from "../workspace/workspaceSessionListRefresh";

function normalizeSessionTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new Error("会话名称不能为空");
  }
  return trimmed;
}

function isCurrentSessionOwner(
  state: Pick<
    AppState,
    "currentSession" | "currentSessionWorkspaceId" | "activeGatewayWorkspaceId"
  >,
  owner: { sessionId: string | null; workspaceId: string | null },
): boolean {
  return (state.currentSession?.session_id ?? null) === owner.sessionId
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId)
      === owner.workspaceId;
}

/** 把一条会话登记为当前会话后的共同落点：复位会话级显示缓存、写入历史加载
 * 状态并追加一条 session_selected 事件。selectSession 与 selectWorkspaceSession
 * 原本逐字复制同一段 25 行，只差事件载荷；新增会话级字段时极易只改一处。 */
function applySessionSelected(
  next: AppState,
  previous: AppState,
  selected: Session,
  cacheKey: string,
  eventPayload: Record<string, unknown>,
): void {
  next.unreadSessionKeys.delete(cacheKey);
  next.traceEvents = [];
  next.llmRequestLogs = [];
  next.llmRequestLogsLoadedAt = null;
  next.llmRequestLogsLoading = previous.contentView === "requests";
  next.llmRequestLogsError = null;
  next.sessionResources = [];
  next.sessionResourcesLoadedAt = null;
  next.sessionResourcesLoading = previous.contentView === "resources";
  next.sessionResourcesError = null;
  next.pendingConversations.delete(cacheKey);
  next.contentView = previous.contentView === "agent" ? "default" : previous.contentView;
  next.status = "正在加载会话历史";
  Object.assign(next, resetAgentStateFields(next));
  writeLastSessionId(selected.session_id);
  appendFrontendEvent(
    next.eventQueuesBySession,
    selected.session_id,
    "session_selected",
    "切换会话",
    eventPayload,
    selected.title,
    cacheKey,
  );
}

/** 把一条会话标记为已读的唯一出口。原本 selectSession 与 selectWorkspaceSession
 * 各写一份逐字相同的 setState 块，未读判定与写回必须只有一处。 */
function clearUnreadSessionKey(state: AppState, cacheKey: string): AppState {
  if (!state.unreadSessionKeys.has(cacheKey)) return state;
  const next = cloneMaps(state);
  next.unreadSessionKeys.delete(cacheKey);
  return next;
}

/**
 * 本地已知的删除闭包：被删会话及其按 `parent_session_id` 可达的全部后代。
 *
 * 后端删除会级联整棵子会话树，本地镜像必须用同一闭包一次性收敛，否则被删子树
 * 会作为幽灵条目残留（甚至被选为新的当前会话）。这是删除成功路径的唯一收敛依据；
 * 不得再为此无条件重拉整个会话列表（§10.3 前端半边）。
 */
function localSessionDeletionClosure(
  sessions: readonly Session[],
  rootSessionId: string,
): Set<string> {
  const childSessionIdsByParent = new Map<string, string[]>();
  for (const session of sessions) {
    const parentSessionId = session.parent_session_id;
    if (!parentSessionId) continue;
    const childSessionIds = childSessionIdsByParent.get(parentSessionId);
    if (childSessionIds) {
      childSessionIds.push(session.session_id);
    } else {
      childSessionIdsByParent.set(parentSessionId, [session.session_id]);
    }
  }

  const deletedSessionIds = new Set<string>([rootSessionId]);
  const pendingSessionIds = [rootSessionId];
  for (let cursor = 0; cursor < pendingSessionIds.length; cursor += 1) {
    const parentSessionId = pendingSessionIds[cursor]!;
    const childSessionIds = childSessionIdsByParent.get(parentSessionId);
    if (!childSessionIds) continue;
    for (const childSessionId of childSessionIds) {
      if (deletedSessionIds.has(childSessionId)) continue;
      deletedSessionIds.add(childSessionId);
      pendingSessionIds.push(childSessionId);
    }
  }
  return deletedSessionIds;
}

/**
 * 会话已消失后的统一收敛：按权威剩余列表收敛工作区镜像，并在当前会话确实已从
 * 剩余列表中消失（含被删父会话级联带走的后代）时清空或切到剩余首个会话。
 *
 * 删除成功路径与「请求报错但补偿重取成功」的失败路径必须共用这一份：失败路径若
 * 只收敛 sessions 镜像而不校正 currentSession，就会出现 currentSession 悬空指向
 * 一个已不在 sessions 里的幽灵会话（与成功路径行为不一致）。
 *
 * `reloadHistoryAfterSwitch` 只在「删除非当前会话、但级联带走了当前会话」时为真，
 * 让新选中的会话触发一次历史加载；删除当前会话时调用方已先行切走，无需重复。
 */
function convergeAfterSessionRemoval(
  prev: AppState,
  workspaceId: string,
  remainingSessions: Session[],
  options: { reloadHistoryAfterSwitch: boolean; selectNextSession: boolean },
): AppState {
  const next = applySessionListConvergence(prev, workspaceId, remainingSessions);
  const remainingIds = new Set(
    remainingSessions.map((session) => session.session_id),
  );
  const currentWasDeleted = Boolean(
    prev.currentSession
    && (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) === workspaceId
    && !remainingIds.has(prev.currentSession.session_id),
  );
  if (!currentWasDeleted) {
    return next;
  }
  const nextSession = options.selectNextSession ? remainingSessions[0] ?? null : null;
  next.currentSession = nextSession;
  next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
  if (options.reloadHistoryAfterSwitch) {
    next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
  }
  Object.assign(next, resetSessionScopedFields(next));
  next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
  Object.assign(next, resetAgentStateFields(next));
  if (nextSession) {
    writeLastSessionId(nextSession.session_id);
  } else {
    clearLastSessionId();
  }
  return next;
}

/** 目录版本失效时只核对已打开 owner；deleting 的 DTO 仍可读取，404 才收敛删除。 */
export async function refreshCurrentSessionCatalogOwner(
  apiPort: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  readState: () => AppState,
  setState: SetAppState,
  signal: AbortSignal,
): Promise<void> {
  const before = readState();
  const sessionId = before.currentSession?.session_id;
  const workspaceId = workspaceTarget.workspace_id;
  if (!sessionId || (before.currentSessionWorkspaceId ?? before.activeGatewayWorkspaceId) !== workspaceId) {
    return;
  }
  const stillCurrent = (state: AppState) => !signal.aborted
    && state.currentSession?.session_id === sessionId
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId) === workspaceId
    && state.apiPort === before.apiPort
    && state.gatewayUserAccess?.kind === before.gatewayUserAccess?.kind
    && state.gatewayUserAccess?.user_id === before.gatewayUserAccess?.user_id;
  let updatedSession: Session | null = null;
  try {
    updatedSession = await apiGetSession(apiPort, sessionId, workspaceId, { signal });
    if (updatedSession.session_id !== sessionId
      || updatedSession.workspace_id !== workspaceTarget.backend_workspace_id) {
      throw new Error(`当前会话目录对账返回了其它 owner: ${workspaceId}/${sessionId}`);
    }
  } catch (error: unknown) {
    if (!(error instanceof HttpRequestError) || error.status !== 404) throw error;
  }
  setState((previous) => {
    if (!stillCurrent(previous)) return previous;
    if (updatedSession) return replaceSessionMetadata(previous, updatedSession, workspaceId);
    const sessions = previous.sessionsByWorkspace.get(workspaceId)
      ?? (previous.activeGatewayWorkspaceId === workspaceId ? previous.sessions : []);
    const next = convergeAfterSessionRemoval(previous, workspaceId,
      sessions.filter((session) => session.session_id !== sessionId),
      { reloadHistoryAfterSwitch: true, selectNextSession: false });
    next.status = `当前会话已删除: ${sessionId}`;
    return next;
  });
}

export function useSessionLifecycleActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  defaultGatewayWorkspaceId,
  readState,
  setState,
  abortCurrentStream,
  invalidateAgentState,
  sessionCatalogOutboxRuntime,
  gatewayUserAccess,
  workspaceRequestTargets,
  selectionIntentRef,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  defaultGatewayWorkspaceId: string | null;
  readState: () => AppState;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;
  gatewayUserAccess?: GatewayUserAccess | null;
  workspaceRequestTargets: readonly GatewayWorkspaceForkRoute[];
  selectionIntentRef: MutableRefObject<number>;
}) {
  // 仅在已提交的 owner 离开时作废旧请求；React 合并 A→B→A 时保留 A 的订阅。
  useEffect(() => () => invalidateAgentState(), [
    currentSession?.session_id,
    currentSessionGatewayWorkspaceId,
    invalidateAgentState,
  ]);
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

  const selectSession = useCallback(
    (sessionId: string) => {
      selectionIntentRef.current += 1;
      const snapshot = readState();
      // 合法选择由订阅的 owner effect 清理旧连接；未知选择保留原中断合同。
      if (snapshot.currentSession?.session_id !== sessionId
        && !snapshot.sessions.some((session) => session.session_id === sessionId)) {
        abortCurrentStream();
        invalidateAgentState();
      }
      setState((prev) => {
        const workspaceId = prev.activeGatewayWorkspaceId;
        const cacheKey = sessionScopeKey(workspaceId, sessionId);
        if (prev.currentSession?.session_id === sessionId
          && (prev.currentSessionWorkspaceId ?? workspaceId) === workspaceId) {
          return clearUnreadSessionKey(prev, cacheKey);
        }
        const selected =
          prev.sessions.find((session) => session.session_id === sessionId);
        if (!selected) {
          // 未命中会话必须显式失败：旧实现用 `?? prev.currentSession` 静默回退，
          // 会带着「切换成功」的全套副作用把用户留在原会话上。
          return {
            ...prev,
            status: `切换会话失败: 不存在会话 ${sessionId}`,
          };
        }
        const next = cloneMaps(prev);
        next.currentSession = selected;
        next.currentSessionWorkspaceId = workspaceId;
        if (workspaceId) {
          next.sessionGatewayWorkspaceById.set(
            cacheKey,
            workspaceId,
          );
        }
        applySessionSelected(next, prev, selected, cacheKey, {
          session_id: selected.session_id,
          title: selected.title,
        });
        return next;
      });
    },
    [
      abortCurrentStream,
      invalidateAgentState,
      readState,
      setState,
      selectionIntentRef,
    ],
  );

  const selectWorkspaceSession = useCallback(
    (
      workspaceId: string,
      sessionId: string,
      sessionOverride?: Session,
    ) => {
      selectionIntentRef.current += 1;
      const snapshot = readState();
      const alreadyCurrent = isCurrentSessionOwner(snapshot, { sessionId, workspaceId })
        && snapshot.activeGatewayWorkspaceId === workspaceId;
      const selected = sessionOverride ?? snapshot.sessionsByWorkspace.get(workspaceId)?.find(
        (session) => session.session_id === sessionId,
      );
      const workspaceKnown = workspaceId === snapshot.activeGatewayWorkspaceId
        || snapshot.gatewayWorkspaces.some((workspace) => workspace.workspace_id === workspaceId);
      if (!alreadyCurrent && (!selected || !workspaceKnown)) {
        abortCurrentStream();
        invalidateAgentState();
      }
      setState((prev) => {
        if (prev.currentSession?.session_id === sessionId
          && (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) === workspaceId
          && prev.activeGatewayWorkspaceId === workspaceId) {
          return clearUnreadSessionKey(prev, sessionScopeKey(workspaceId, sessionId));
        }
        const workspaceSessions = prev.sessionsByWorkspace.get(workspaceId) ?? [];
        const selected = sessionOverride ?? workspaceSessions.find(
          (session) => session.session_id === sessionId,
        );
        if (!selected) {
          return {
            ...prev,
            status: `切换会话失败: 工作区 ${workspaceId} 中不存在会话 ${sessionId}`,
          };
        }

        const workspace = prev.gatewayWorkspaces.find(
          (item) => item.workspace_id === workspaceId,
        );
        if (!workspace && workspaceId !== prev.activeGatewayWorkspaceId) {
          // 未知身份不能沿用上一个工作区的展示信息。
          return {
            ...prev,
            status: `切换会话失败: 未知工作区 ${workspaceId}`,
          };
        }
        const next = cloneMaps(prev);
        const nextWorkspaceSessions = [
          selected,
          ...workspaceSessions.filter(
            (session) => session.session_id !== selected.session_id,
          ),
        ];
        next.activeGatewayWorkspaceId = workspaceId;
        if (workspace) {
          next.workspaceName = workspace.name;
        }
        next.sessions = nextWorkspaceSessions;
        next.sessionsByWorkspace.set(workspaceId, nextWorkspaceSessions);
        next.currentSession = selected;
        next.currentSessionWorkspaceId = workspaceId;
        const cacheKey = sessionScopeKey(workspaceId, selected.session_id);
        next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
        next.workspaceSwitching = false;
        next.error = null;
        applySessionSelected(next, prev, selected, cacheKey, {
          session_id: selected.session_id,
          title: selected.title,
          workspace_id: workspaceId,
        });
        return next;
      });
    },
    [
      abortCurrentStream,
      invalidateAgentState,
      readState,
      setState,
      selectionIntentRef,
    ],
  );

  const createSession = useCallback(
    async (
      title: string = DEFAULT_SESSION_TITLE,
      workspaceId?: string | null,
      folderId?: string | null,
    ) => {
      const selectionIntent = ++selectionIntentRef.current;
      invalidateAgentState();
      const targetWorkspaceId =
        workspaceId ?? activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId;
      try {
        // 校验必须在 try 内：否则空标题同步抛出，绕过下面统一失败上报，
        // 调用方拿到一个没有对应 status 的错误。
        const normalizedTitle = normalizeSessionTitle(title);
        const session = await apiCreateSession(
          apiPort,
          normalizedTitle,
          targetWorkspaceId,
          folderId,
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          const resolvedWorkspaceId =
            targetWorkspaceId ?? prev.activeGatewayWorkspaceId;
          const workspace = prev.gatewayWorkspaces.find(
            (item) => item.workspace_id === resolvedWorkspaceId,
          );
          const previousWorkspaceSessions = resolvedWorkspaceId
            ? prev.sessionsByWorkspace.get(resolvedWorkspaceId) ?? []
            : prev.sessions;
          const workspaceSessions = [
            session,
            ...previousWorkspaceSessions.filter(
              (item) => item.session_id !== session.session_id,
            ),
          ];
          if (resolvedWorkspaceId) {
            next.sessionsByWorkspace.set(resolvedWorkspaceId, workspaceSessions);
            next.sessionGatewayWorkspaceById.set(
              sessionScopeKey(resolvedWorkspaceId, session.session_id),
              resolvedWorkspaceId,
            );
          }
          if (prev.activeGatewayWorkspaceId === resolvedWorkspaceId) next.sessions = workspaceSessions;
          if (selectionIntent === selectionIntentRef.current) {
            next.activeGatewayWorkspaceId = resolvedWorkspaceId;
            next.currentSessionWorkspaceId = resolvedWorkspaceId ?? null;
            next.workspaceName = workspace?.name ?? prev.workspaceName;
            next.sessions = workspaceSessions;
            next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
            next.currentSession = session;
            writeLastSessionId(session.session_id);
            Object.assign(next, resetSessionScopedFields(next));
            next.status = "已创建会话";
            next.contentView = "default";
            Object.assign(next, resetAgentStateFields(next));
          }
          appendFrontendEvent(
            next.eventQueuesBySession,
            session.session_id,
            "session_created",
            "创建会话",
            {
              session_id: session.session_id,
              title: session.title,
            },
            session.title,
            resolvedWorkspaceId
              ? sessionScopeKey(resolvedWorkspaceId, session.session_id)
              : session.session_id,
          );
          return next;
        });
        return session;
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => selectionIntent === selectionIntentRef.current
          ? { ...prev, status: `创建会话失败: ${message}` } : prev);
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      defaultGatewayWorkspaceId,
      invalidateAgentState,
      setState,
    ],
  );

  const forkSessionContext = useCallback(
    async (workspaceId: string, sourceSessionId: string, request: SessionForkRequest) => {
      const selectionIntent = ++selectionIntentRef.current;
      setState((prev) => ({
        ...prev,
        status: "正在创建上下文分支",
      }));

      try {
        const health = await getGatewayHealth(apiPort);
        const result = await apiForkSessionContext(
          apiPort,
          sourceSessionId,
          request,
          workspaceId,
        );
        const targetWorkspaceId = requireForkTargetWorkspace(
          workspaceRequestTargets, health.gateway_id, result,
        ).workspace_id;
        setState((prev) => {
          const next = cloneMaps(prev);
          const sessions = prev.sessionsByWorkspace.get(targetWorkspaceId) ?? [];
          const targetSessions = [result.session, ...sessions.filter((session) =>
            session.session_id !== result.session.session_id,
          )];
          next.sessionsByWorkspace.set(targetWorkspaceId, targetSessions);
          if (prev.activeGatewayWorkspaceId === targetWorkspaceId) next.sessions = targetSessions;
          if (isCurrentSessionOwner(prev, { sessionId: result.session.session_id, workspaceId: targetWorkspaceId })) {
            next.currentSession = result.session;
          }
          if (selectionIntent === selectionIntentRef.current) next.status = `已创建上下文分支: ${result.thread_id}`;
          return next;
        });
        return result;
      } catch (error) {
        const message = errorMessage(error);
        // 补偿重取失败不得覆盖原始错误：列表服务不可用时抛出的是 503，
        // 而用户真正需要看到的是 fork 失败本身（如 422 上下文快照损坏）。
        try {
          const refreshed = await apiListSessions(apiPort, workspaceId);
          setState((prev) => {
            const next = cloneMaps(prev);
            next.sessionsByWorkspace.set(workspaceId, refreshed.items);
            if (prev.activeGatewayWorkspaceId === workspaceId) {
              next.sessions = refreshed.items;
            }
            if (selectionIntent === selectionIntentRef.current) next.status = `创建上下文分支失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            status: `创建上下文分支失败: ${message}；重新读取会话列表也失败: ${reconciliationMessage}`,
          }) : prev);
        }
        throw error;
      }
    },
    [apiPort, setState, selectionIntentRef, workspaceRequestTargets],
  );

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
          if (isCurrentSessionOwner(prev, statusOwner)) {
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
        setState((prev) => isCurrentSessionOwner(prev, statusOwner)
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
        const doomed = localSessionDeletionClosure(previousSessions, sessionId);
        const remainingSessions = previousSessions.filter(
          (session) => !doomed.has(session.session_id),
        );
        const next = convergeAfterSessionRemoval(
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

  const switchAgent = useCallback(
    async (agentId: string) => {
      const session = currentSession;
      if (!session) {
        throw new Error("当前没有可切换 Agent 的会话");
      }

      if (agentId === session.current_agent_id) {
        setState((prev) => ({ ...prev, status: `当前已是 Agent: ${agentId}` }));
        return;
      }

      const statusOwner = {
        sessionId: session.session_id,
        workspaceId: currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId,
      };

      setState((prev) => ({ ...prev, status: `正在切换 Agent: ${agentId}` }));

      try {
        const updatedSession = await apiUpdateSessionAgent(
          apiPort,
          session.session_id,
          agentId,
          currentSessionGatewayWorkspaceId,
        );
        setState((prev) => {
          // 只改会话元数据，且仅当用户仍停在被切换的会话上时才写
          // currentSession（replaceSessionMetadata 的内部守卫）。直接赋值会把
          // 请求在途期间用户已经切走的会话拉回来。
          const workspaceId =
            currentSessionGatewayWorkspaceId ??
            prev.activeGatewayWorkspaceId ??
            updatedSession.workspace_id;
          const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
          if (isCurrentSessionOwner(prev, statusOwner)) {
            next.status = `已切换 Agent: ${updatedSession.current_agent_id}`;
          }
          const cacheKey = sessionScopeKey(workspaceId, updatedSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "agent_switched",
            "切换 Agent",
            {
              session_id: updatedSession.session_id,
              agent_id: updatedSession.current_agent_id,
            },
            updatedSession.current_agent_id,
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        let message = errorMessage(error);
        // 失败后重取会话校准：切换可能已生效（如响应超时），也可能是别的
        // 来源改了 current_agent_id，本地镜像必须以后端真值为准。
        try {
          const refreshed = await apiGetSession(
            apiPort,
            session.session_id,
            currentSessionGatewayWorkspaceId,
          );
          setState((prev) => {
            const next = replaceSessionMetadata(
              prev,
              refreshed,
              currentSessionGatewayWorkspaceId,
            );
            if (isCurrentSessionOwner(prev, statusOwner)) {
              next.status = `Agent 切换失败: ${message}`;
            }
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => isCurrentSessionOwner(prev, statusOwner)
            ? { ...prev, status: `Agent 切换失败: ${message}` }
            : prev);
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSession,
      currentSessionGatewayWorkspaceId,
      setState,
    ],
  );

  const switchModel = useCallback(
    async (providerId: string) => {
      const session = currentSession ?? await createSession(DEFAULT_SESSION_TITLE);
      if (providerId === session.current_provider_id) {
        setState((prev) => ({
          ...prev,
          status: `当前已使用模型 provider: ${providerId}`,
        }));
        return;
      }

      const statusOwner = {
        sessionId: session.session_id,
        workspaceId:
          currentSessionGatewayWorkspaceId
          ?? activeGatewayWorkspaceId
          ?? defaultGatewayWorkspaceId,
      };

      setState((prev) => ({
        ...prev,
        status: `正在切换模型 provider: ${providerId}`,
      }));
      try {
        const updatedSession = await apiUpdateSessionProvider(
          apiPort,
          session.session_id,
          providerId,
          currentSessionGatewayWorkspaceId
            ?? activeGatewayWorkspaceId
            ?? defaultGatewayWorkspaceId,
        );
        setState((prev) => {
          const next = replaceSessionMetadata(
            prev,
            updatedSession,
            currentSessionGatewayWorkspaceId
              ?? activeGatewayWorkspaceId
              ?? defaultGatewayWorkspaceId,
          );
          if (isCurrentSessionOwner(prev, statusOwner)) {
            next.status = `已切换模型 provider: ${updatedSession.current_provider_id}`;
          }
          const workspaceId =
            currentSessionGatewayWorkspaceId
            ?? activeGatewayWorkspaceId
            ?? defaultGatewayWorkspaceId
            ?? prev.activeGatewayWorkspaceId
            ?? updatedSession.workspace_id;
          const cacheKey = sessionScopeKey(workspaceId, updatedSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "model_switched",
            "切换模型",
            {
              session_id: updatedSession.session_id,
              provider_id: updatedSession.current_provider_id,
            },
            updatedSession.current_provider_id ?? "",
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        let message = errorMessage(error);
        try {
          const refreshed = await apiGetSession(
            apiPort,
            session.session_id,
            currentSessionGatewayWorkspaceId
              ?? activeGatewayWorkspaceId
              ?? defaultGatewayWorkspaceId,
          );
          setState((prev) => {
            const next = replaceSessionMetadata(
              prev,
              refreshed,
              currentSessionGatewayWorkspaceId
                ?? activeGatewayWorkspaceId
                ?? defaultGatewayWorkspaceId,
            );
            if (isCurrentSessionOwner(prev, statusOwner)) {
              next.status = `模型切换失败: ${message}`;
            }
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => isCurrentSessionOwner(prev, statusOwner)
            ? { ...prev, status: `模型切换失败: ${message}` }
            : prev);
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      createSession,
      currentSession,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const setWorkspaceDefaultAgent = useCallback(
    async (agentId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认 Agent 的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认 Agent: ${agentId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultAgent(
          apiPort,
          agentId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${agentId} 设为工作区默认 Agent，仅影响新会话`,
        }));
      } catch (error) {
        const message = errorMessage(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认 Agent 失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认 Agent 失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const setWorkspaceDefaultProvider = useCallback(
    async (agentId: string, providerId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认模型的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认模型: ${providerId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultProvider(
          apiPort,
          agentId,
          providerId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${providerId} 设为 ${agentId} 的工作区默认模型，仅影响新会话`,
        }));
      } catch (error) {
        const message = errorMessage(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认模型失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认模型失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  return {
    createSession,
    deleteSession,
    forkSessionContext,
    renameSession,
    setSessionParent,
    selectSession,
    selectWorkspaceSession,
    switchAgent,
    switchModel,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
  };
}
