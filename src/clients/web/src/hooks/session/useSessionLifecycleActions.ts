import { useCallback } from "react";
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
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { cloneMaps } from "../../state/appStateMaps";
import {
  clearLastSessionId,
  writeLastSessionId,
} from "../../state/storage";
import { replaceSessionMetadata } from "../../state/session/sessions";
import { appendFrontendEvent } from "../../state/traceEvents";
import { resetAgentStateFields } from "../runtime/useAgentStateSnapshot";
import { resetSessionScopedFields } from "./sessionScopedState";
import type { SetAppState } from "../contentViewLoaderTypes";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { errorMessage } from "../../utils/errorMessage";
import {
  requireGatewayWorkspaceRequestTarget,
  type GatewayWorkspaceRequestTarget,
} from "../../api/workspaceIdentity";
import type { GatewayUserAccess } from "../../types/backend";
import {
  catalogOutboxOperationIsTerminal,
  type CatalogOutboxIntent,
  type CatalogOutboxOperation,
} from "../../state/session/sessionCatalogOutbox";
import type { SessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";

function normalizeSessionTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new Error("会话名称不能为空");
  }
  return trimmed;
}

/** 从会话镜像收敛时，移除已消失会话的全部会话级附属缓存。 */
function purgeSessionScopedCaches(
  state: AppState,
  sessionId: string,
  cacheKey: string,
): void {
  state.sessionAttachmentSummaries.delete(sessionId);
  state.eventQueuesBySession.delete(cacheKey);
  state.sessionTraceHistoryBySession.delete(cacheKey);
  state.pendingConversations.delete(cacheKey);
  state.activeJobIdsBySession.delete(cacheKey);
  state.unreadSessionKeys.delete(cacheKey);
  state.sessionGatewayWorkspaceById.delete(cacheKey);
  state.turnTimelinesBySession.delete(cacheKey);
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
function clearUnreadSessionKey(setState: SetAppState, cacheKey: string): void {
  setState((prev) => {
    if (!prev.unreadSessionKeys.has(cacheKey)) {
      return prev;
    }
    const next = cloneMaps(prev);
    next.unreadSessionKeys.delete(cacheKey);
    return next;
  });
}

/** 用权威会话列表整表替换本地镜像，并清掉已消失会话的附属缓存。 */
function applySessionListConvergence(
  state: AppState,
  workspaceId: string,
  remainingSessions: Session[],
): AppState {
  const next = cloneMaps(state);
  const previousSessions =
    state.sessionsByWorkspace.get(workspaceId) ?? state.sessions;
  // 全局 sessions 镜像只描述当前活动工作区（selectWorkspaceSession /
  // forkSessionContext 都先切 activeGatewayWorkspaceId 再写它）。收敛一个非
  // 活动工作区的会话列表时若整表覆盖全局镜像，会把该工作区的会话灌进来。
  if (state.activeGatewayWorkspaceId === workspaceId) {
    next.sessions = remainingSessions;
  }
  next.sessionsByWorkspace.set(workspaceId, remainingSessions);
  const remainingIds = new Set(
    remainingSessions.map((session) => session.session_id),
  );
  for (const removed of previousSessions) {
    if (remainingIds.has(removed.session_id)) continue;
    purgeSessionScopedCaches(
      next,
      removed.session_id,
      sessionScopeKey(workspaceId, removed.session_id),
    );
  }
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
  const doomed = new Set<string>([rootSessionId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const session of sessions) {
      if (doomed.has(session.session_id)) continue;
      const parentId = session.parent_session_id;
      if (parentId && doomed.has(parentId)) {
        doomed.add(session.session_id);
        changed = true;
      }
    }
  }
  return doomed;
}

/**
 * 会话已消失后的统一收敛：按权威剩余列表收敛工作区镜像，并在当前会话确实已从
 * 剩余列表中消失（含被删父会话级联带走的后代）时把它切到剩余首个会话。
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
  options: { reloadHistoryAfterSwitch: boolean },
): AppState {
  const next = applySessionListConvergence(prev, workspaceId, remainingSessions);
  const remainingIds = new Set(
    remainingSessions.map((session) => session.session_id),
  );
  const currentWasDeleted = Boolean(
    prev.currentSession
    && !remainingIds.has(prev.currentSession.session_id),
  );
  if (!currentWasDeleted) {
    return next;
  }
  const nextSession = remainingSessions[0] ?? null;
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

export function useSessionLifecycleActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  setState,
  abortCurrentStream,
  invalidateAgentState,
  sessionCatalogOutboxRuntime,
  gatewayUserAccess,
  workspaceRequestTargets,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;
  gatewayUserAccess?: GatewayUserAccess | null;
  workspaceRequestTargets: readonly GatewayWorkspaceRequestTarget[];
}) {
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
    const { handle, operationId, dispatchOutcome } = await sessionCatalogOutboxRuntime.submit(
      workspaceRequestTarget(workspaceId),
      gatewayUserAccess,
      intent,
      {
        baseCatalogRevision,
        ...(expectedRevision === undefined ? {} : { expectedRevision }),
      },
    );
    if (dispatchOutcome === "accepted") {
      setState((prev) => ({ ...prev, status: "会话目录操作已持久受理，等待提交" }));
    } else if (dispatchOutcome === "unknown") {
      setState((prev) => ({ ...prev, status: "会话目录操作状态待确认，保留本地待同步变更" }));
    }
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
      if (
        currentSession?.session_id === sessionId
        && currentSessionGatewayWorkspaceId === activeGatewayWorkspaceId
      ) {
        const cacheKey = currentSessionGatewayWorkspaceId
          ? sessionScopeKey(currentSessionGatewayWorkspaceId, sessionId)
          : sessionId;
        clearUnreadSessionKey(setState, cacheKey);
        return;
      }
      abortCurrentStream();
      invalidateAgentState();
      setState((prev) => {
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
        const workspaceId = prev.activeGatewayWorkspaceId;
        next.currentSessionWorkspaceId = workspaceId;
        const cacheKey = workspaceId
          ? sessionScopeKey(workspaceId, selected.session_id)
          : selected.session_id;
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
      activeGatewayWorkspaceId,
      currentSession?.session_id,
      currentSessionGatewayWorkspaceId,
      invalidateAgentState,
      setState,
    ],
  );

  const selectWorkspaceSession = useCallback(
    (
      workspaceId: string,
      sessionId: string,
      sessionOverride?: Session,
    ) => {
      if (
        currentSession?.session_id === sessionId
        && currentSessionGatewayWorkspaceId === workspaceId
        && activeGatewayWorkspaceId === workspaceId
      ) {
        const cacheKey = sessionScopeKey(workspaceId, sessionId);
        clearUnreadSessionKey(setState, cacheKey);
        return;
      }
      abortCurrentStream();
      invalidateAgentState();
      setState((prev) => {
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
          // 目标工作区既不在 Gateway 列表里、也不是当前活动工作区时，它的
          // 根目录/名称无从得知。旧实现用 `?? prev.workspaceRoot/Name` 沿用
          // 上一个工作区的元数据，把「未知工作区」伪装成切换成功，后续文件树
          // 与预览会指向错误的根目录。等于活动工作区时沿用是正确的（那些
          // 字段描述的就是它），因此只对真正未知的目标失败。
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
          next.workspaceRoot = workspace.root_path;
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
      activeGatewayWorkspaceId,
      currentSession?.session_id,
      currentSessionGatewayWorkspaceId,
      invalidateAgentState,
      setState,
    ],
  );

  const createSession = useCallback(
    async (
      title: string = DEFAULT_SESSION_TITLE,
      workspaceId?: string | null,
      folderId?: string | null,
    ) => {
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
          next.activeGatewayWorkspaceId = resolvedWorkspaceId;
          next.currentSessionWorkspaceId = resolvedWorkspaceId ?? null;
          next.workspaceRoot = workspace?.root_path ?? prev.workspaceRoot;
          next.workspaceName = workspace?.name ?? prev.workspaceName;
          const previousWorkspaceSessions = resolvedWorkspaceId
            ? prev.sessionsByWorkspace.get(resolvedWorkspaceId) ?? []
            : prev.sessions;
          next.sessions = [
            session,
            ...previousWorkspaceSessions.filter(
              (item) => item.session_id !== session.session_id,
            ),
          ];
          if (resolvedWorkspaceId) {
            next.sessionsByWorkspace.set(resolvedWorkspaceId, next.sessions);
            next.sessionGatewayWorkspaceById.set(
              sessionScopeKey(resolvedWorkspaceId, session.session_id),
              resolvedWorkspaceId,
            );
          }
          next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
          next.currentSession = session;
          writeLastSessionId(session.session_id);
          Object.assign(next, resetSessionScopedFields(next));
          next.status = "已创建会话";
          next.contentView = "default";
          Object.assign(next, resetAgentStateFields(next));
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
        setState((prev) => ({ ...prev, status: `创建会话失败: ${message}` }));
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
    async (workspaceId: string, sourceSessionId: string) => {
      setState((prev) => ({
        ...prev,
        status: "正在复制 Agent 上下文并创建子会话",
      }));

      try {
        const childSession = await apiForkSessionContext(
          apiPort,
          sourceSessionId,
          true,
          workspaceId,
        );
        abortCurrentStream();
        invalidateAgentState();
        setState((prev) => {
          const next = cloneMaps(prev);
          const workspace = prev.gatewayWorkspaces.find(
            (item) => item.workspace_id === workspaceId,
          );
          const workspaceSessions = [
            childSession,
            ...(prev.sessionsByWorkspace.get(workspaceId) ?? []).filter(
              (item) => item.session_id !== childSession.session_id,
            ),
          ];
          const cacheKey = sessionScopeKey(
            workspaceId,
            childSession.session_id,
          );

          next.activeGatewayWorkspaceId = workspaceId;
          next.currentSessionWorkspaceId = workspaceId;
          next.workspaceRoot = workspace?.root_path ?? prev.workspaceRoot;
          next.workspaceName = workspace?.name ?? prev.workspaceName;
          next.sessions = workspaceSessions;
          next.sessionsByWorkspace.set(workspaceId, workspaceSessions);
          next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
          next.currentSession = childSession;
          next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
          Object.assign(next, resetSessionScopedFields(next));
          next.pendingConversations.delete(cacheKey);
          next.contentView = "default";
          next.status = `已从上下文创建子会话: ${childSession.title}`;
          Object.assign(next, resetAgentStateFields(next));
          writeLastSessionId(childSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            childSession.session_id,
            "session_context_forked",
            "从上下文创建子会话",
            {
              session_id: childSession.session_id,
              parent_session_id: sourceSessionId,
            },
            childSession.title,
            cacheKey,
          );
          return next;
        });
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
            next.status = `从上下文创建子会话失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `从上下文创建子会话失败: ${message}；重新读取会话列表也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [abortCurrentStream, apiPort, invalidateAgentState, setState],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string, workspaceId?: string | null) => {
      const workspaceIdForRequest =
        workspaceId ?? currentSessionGatewayWorkspaceId;
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
          next.currentSessionWorkspaceId =
            workspaceIdForRequest ?? next.currentSessionWorkspaceId;
          next.status = `已命名会话: ${updatedSession.title}`;
          const cacheKey =
            currentSessionCacheKey ??
            (workspaceIdForRequest
              ? sessionScopeKey(
                  workspaceIdForRequest,
                  updatedSession.session_id,
                )
              : updatedSession.session_id);
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
        setState((prev) => ({ ...prev, status: `会话命名失败: ${message}` }));
        throw error;
      }
    },
    [
      apiPort,
      commitCatalogMutation,
      currentSessionCacheKey,
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
      let catalogRefreshFailure: string | null = null;
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
          catalogRefreshFailure = errorMessage(error);
          sessionCatalogOutboxRuntime?.reportError(
            `会话目录操作 ${operation.client_operation_id} 已到终态但目录分支刷新失败: `
            + catalogRefreshFailure,
          );
        }
        if (operation.state !== "committed") {
          throw terminalError ?? new Error(
            `会话目录删除被 ${operation.state}: `
            + `${operation.error_code ?? "unknown"} ${operation.error_detail ?? ""}`.trim(),
          );
        }
        if (terminalError && !catalogRefreshFailure) {
          sessionCatalogOutboxRuntime.reportError(
            `会话目录操作 ${operationId} 已提交且分支已刷新，但终态确认异常: `
            + errorMessage(terminalError),
          );
        }
      } catch (error) {
        const message = errorMessage(error);
        const refreshDetail = catalogRefreshFailure
          ? `；目录分支刷新失败: ${catalogRefreshFailure}`
          : "";
        setState((prev) => ({
          ...prev,
          status: `删除会话失败: ${message}${refreshDetail}`,
        }));
        throw error;
      }

      const resolvedWorkspaceId = workspaceIdForRequest;
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
          { reloadHistoryAfterSwitch: true },
        );

        next.status = catalogRefreshFailure
          ? `已删除会话: ${sessionId}；会话目录分支刷新失败: ${catalogRefreshFailure}`
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
          next.status = `已切换 Agent: ${updatedSession.current_agent_id}`;
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
            next.status = `Agent 切换失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => ({ ...prev, status: `Agent 切换失败: ${message}` }));
        }
        throw error;
      }
    },
    [apiPort, currentSession, currentSessionGatewayWorkspaceId, setState],
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
          next.status = `已切换模型 provider: ${updatedSession.current_provider_id}`;
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
            next.status = `模型切换失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => ({ ...prev, status: `模型切换失败: ${message}` }));
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
