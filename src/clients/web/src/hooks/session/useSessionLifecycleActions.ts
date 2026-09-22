import { useCallback } from "react";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  deleteSession as apiDeleteSession,
  forkSessionContext as apiForkSessionContext,
  getSession as apiGetSession,
  listAgents as apiListAgents,
  listSessions as apiListSessions,
  moveSessionParent as apiMoveSessionParent,
  setWorkspaceDefaultAgent as apiSetWorkspaceDefaultAgent,
  setWorkspaceDefaultProvider as apiSetWorkspaceDefaultProvider,
  updateSession as apiUpdateSession,
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
import type { SetAppState } from "../contentViewLoaderTypes";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { errorMessage } from "../../utils/errorMessage";

function normalizeSessionTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new Error("会话名称不能为空");
  }
  return trimmed;
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
  next.sessions = remainingSessions;
  next.sessionsByWorkspace.set(workspaceId, remainingSessions);
  const remainingIds = new Set(
    remainingSessions.map((session) => session.session_id),
  );
  for (const removed of previousSessions) {
    if (remainingIds.has(removed.session_id)) continue;
    const cacheKey = sessionScopeKey(workspaceId, removed.session_id);
    next.sessionAttachmentSummaries.delete(removed.session_id);
    next.eventQueuesBySession.delete(cacheKey);
    next.pendingConversations.delete(cacheKey);
    next.activeJobIdsBySession.delete(cacheKey);
    next.unreadSessionKeys.delete(cacheKey);
    next.sessionGatewayWorkspaceById.delete(cacheKey);
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
}) {
  const selectSession = useCallback(
    (sessionId: string) => {
      if (
        currentSession?.session_id === sessionId
        && currentSessionGatewayWorkspaceId === activeGatewayWorkspaceId
      ) {
        const cacheKey = currentSessionGatewayWorkspaceId
          ? sessionScopeKey(currentSessionGatewayWorkspaceId, sessionId)
          : sessionId;
        setState((prev) => {
          if (!prev.unreadSessionKeys.has(cacheKey)) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.unreadSessionKeys.delete(cacheKey);
          return next;
        });
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
        next.unreadSessionKeys.delete(cacheKey);
        next.traceEvents = [];
        next.llmRequestLogs = [];
        next.llmRequestLogsLoadedAt = null;
        next.llmRequestLogsLoading = prev.contentView === "requests";
        next.llmRequestLogsError = null;
        next.sessionResources = [];
        next.sessionResourcesLoadedAt = null;
        next.sessionResourcesLoading = prev.contentView === "resources";
        next.sessionResourcesError = null;
        next.pendingConversations.delete(cacheKey);
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        next.status = "正在加载会话历史";
        Object.assign(next, resetAgentStateFields(next));
        writeLastSessionId(selected.session_id);
        appendFrontendEvent(
          next.eventQueuesBySession,
          selected.session_id,
          "session_selected",
          "切换会话",
          {
            session_id: selected.session_id,
            title: selected.title,
          },
          selected.title,
          cacheKey,
        );
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
        setState((prev) => {
          if (!prev.unreadSessionKeys.has(cacheKey)) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.unreadSessionKeys.delete(cacheKey);
          return next;
        });
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
        next.unreadSessionKeys.delete(cacheKey);
        next.traceEvents = [];
        next.llmRequestLogs = [];
        next.llmRequestLogsLoadedAt = null;
        next.llmRequestLogsLoading = prev.contentView === "requests";
        next.llmRequestLogsError = null;
        next.sessionResources = [];
        next.sessionResourcesLoadedAt = null;
        next.sessionResourcesLoading = prev.contentView === "resources";
        next.sessionResourcesError = null;
        next.pendingConversations.delete(cacheKey);
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        next.status = "正在加载会话历史";
        next.workspaceSwitching = false;
        next.error = null;
        Object.assign(next, resetAgentStateFields(next));
        writeLastSessionId(selected.session_id);
        appendFrontendEvent(
          next.eventQueuesBySession,
          selected.session_id,
          "session_selected",
          "切换会话",
          {
            session_id: selected.session_id,
            title: selected.title,
            workspace_id: workspaceId,
          },
          selected.title,
          cacheKey,
        );
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
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.llmRequestLogsLoading = false;
          next.llmRequestLogsError = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionResourcesLoading = false;
          next.sessionResourcesError = null;
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
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.llmRequestLogsLoading = false;
          next.llmRequestLogsError = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionResourcesLoading = false;
          next.sessionResourcesError = null;
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
      setState((prev) => ({ ...prev, status: "正在命名会话" }));

      try {
        // 与 createSession 同域：校验放在 try 内，空标题必须走统一失败上报。
        const normalizedTitle = normalizeSessionTitle(title);
        const updatedSession = await apiUpdateSession(apiPort, sessionId, {
          title: normalizedTitle,
        }, workspaceIdForRequest);
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
        let message = errorMessage(error);
        // 失败后重取会话校准：重命名可能已生效（如响应超时），本地镜像必须
        // 以后端返回的权威标题为准，而不是停在乐观假设上。
        try {
          const refreshed = await apiGetSession(
            apiPort,
            sessionId,
            workspaceIdForRequest,
          );
          setState((prev) => {
            const next = replaceSessionMetadata(prev, refreshed, workspaceIdForRequest);
            next.currentSessionWorkspaceId =
              workspaceIdForRequest ?? next.currentSessionWorkspaceId;
            next.status = `会话命名失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => ({ ...prev, status: `会话命名失败: ${message}` }));
        }
        throw error;
      }
    },
    [apiPort, currentSessionCacheKey, currentSessionGatewayWorkspaceId, setState],
  );

  const setSessionParent = useCallback(
    async (
      workspaceId: string,
      sessionId: string,
      parentSessionId: string | null,
    ) => {
      setState((prev) => ({
        ...prev,
        status: parentSessionId ? "正在绑定子会话" : "正在解除会话绑定",
      }));

      try {
        const updatedSession = await apiMoveSessionParent(
          apiPort,
          workspaceId,
          sessionId,
          parentSessionId,
        );
        setState((prev) => {
          const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
          next.status = parentSessionId
            ? `已将「${updatedSession.title}」绑定为子会话`
            : `已解除「${updatedSession.title}」的父会话绑定`;
          return next;
        });
      } catch (error) {
        const message = errorMessage(error);
        // 同 forkSessionContext：补偿重取失败必须保留原始错误语义。
        try {
          const refreshed = await apiListSessions(apiPort, workspaceId);
          setState((prev) => {
            const next = cloneMaps(prev);
            next.sessionsByWorkspace.set(workspaceId, refreshed.items);
            if (prev.activeGatewayWorkspaceId === workspaceId) {
              next.sessions = refreshed.items;
              const currentId = prev.currentSession?.session_id;
              next.currentSession = currentId
                ? refreshed.items.find((item) => item.session_id === currentId) ?? null
                : null;
            }
            next.status = `更新会话树失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `更新会话树失败: ${message}；重新读取会话列表也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [apiPort, setState],
  );

  const deleteSession = useCallback(
    async (sessionId: string, workspaceId?: string | null) => {
      const deletingCurrent = currentSession?.session_id === sessionId;
      const workspaceIdForRequest =
        workspaceId ?? currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId;
      if (deletingCurrent) {
        abortCurrentStream();
        invalidateAgentState();
        // 先切走当前会话，再等待删除请求完成。否则删除期间历史、Goal、
        // 资源等 effect 仍会继续请求即将消失的 session，最终把 404/500
        // 写回页面并覆盖用户刚选中的会话。
        setState((previous) => {
          if (previous.currentSession?.session_id !== sessionId) {
            return previous;
          }
          const next = cloneMaps(previous);
          const workspaceSessions = workspaceIdForRequest
            ? previous.sessionsByWorkspace.get(workspaceIdForRequest) ?? previous.sessions
            : previous.sessions;
          const nextSession = workspaceSessions.find(
            (candidate) => candidate.session_id !== sessionId,
          ) ?? null;
          next.currentSession = nextSession;
          next.currentSessionWorkspaceId = nextSession ? workspaceIdForRequest : null;
          next.sessions = workspaceSessions.filter(
            (candidate) => candidate.session_id !== sessionId,
          );
          if (workspaceIdForRequest) {
            next.sessionsByWorkspace.set(workspaceIdForRequest, next.sessions);
          }
          const cacheKey = workspaceIdForRequest
            ? sessionScopeKey(workspaceIdForRequest, sessionId)
            : sessionId;
          next.pendingConversations.delete(cacheKey);
          next.activeJobIdsBySession.delete(cacheKey);
          next.turnTimelinesBySession.delete(cacheKey);
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionHistoryReloadNonce = previous.sessionHistoryReloadNonce + 1;
          next.contentView = previous.contentView === "agent" ? "default" : previous.contentView;
          next.currentGoal = null;
          next.currentGoalSessionId = nextSession?.session_id ?? null;
          next.goalLoading = Boolean(nextSession);
          next.goalError = null;
          Object.assign(next, resetAgentStateFields(next));
          if (nextSession) writeLastSessionId(nextSession.session_id);
          else clearLastSessionId();
          return next;
        });
      }

      setState((prev) => ({ ...prev, status: "正在删除会话" }));

      let result: Awaited<ReturnType<typeof apiDeleteSession>>;
      try {
        result = await apiDeleteSession(
          apiPort,
          sessionId,
          workspaceIdForRequest,
          true,
        );
      } catch (error) {
        const message = errorMessage(error);
        // 失败后主动从后端重取列表校准本地镜像（AGENTS.md 前端状态管理第 4 条）：
        // 删除可能实际已在后端生效（如响应超时），本地必须以后端为准。
        let reconciled: Session[] | null = null;
        let reconciliationFailure: string | null = null;
        try {
          reconciled = (
            await apiListSessions(apiPort, workspaceIdForRequest)
          ).items;
        } catch (reconciliationError) {
          reconciliationFailure = errorMessage(reconciliationError);
        }
        setState((prev) => {
          const resolvedWorkspaceId =
            workspaceIdForRequest ??
            prev.activeGatewayWorkspaceId ??
            "workspace";
          const converged = reconciled
            ? applySessionListConvergence(prev, resolvedWorkspaceId, reconciled)
            : prev;
          return {
            ...converged,
            status: reconciliationFailure
              ? `删除会话失败: ${message}；重新读取会话列表也失败: ${reconciliationFailure}`
              : `删除会话失败: ${message}`,
          };
        });
        throw error;
      }

      // 删除已成功，业务态到此确定。列表刷新只是收敛手段，它失败不得被
      // 表述成「删除失败」——那会让用户对已经生效的删除重试。
      let refreshed: Awaited<ReturnType<typeof apiListSessions>> | null = null;
      let refreshFailure: string | null = null;
      try {
        refreshed = await apiListSessions(apiPort, workspaceIdForRequest);
      } catch (error) {
        refreshFailure = errorMessage(error);
      }

      setState((prev) => {
        const workspaceId =
          workspaceIdForRequest ??
          prev.activeGatewayWorkspaceId ??
          "workspace";
        const previousSessions =
          prev.sessionsByWorkspace.get(workspaceId) ?? prev.sessions;
        // 刷新失败时按已知事实本地收敛：被删会话必须立即从列表消失。
        const remainingSessions = refreshed
          ? refreshed.items
          : previousSessions.filter(
              (session) => session.session_id !== sessionId,
            );
        const next = applySessionListConvergence(
          prev,
          workspaceId,
          remainingSessions,
        );
        const remainingIds = new Set(remainingSessions.map((session) => session.session_id));

        const currentWasDeleted = deletingCurrent
          ? prev.currentSession === null || prev.currentSession.session_id === sessionId
          : Boolean(
            prev.currentSession
            && !remainingIds.has(prev.currentSession.session_id),
          );
        if (currentWasDeleted) {
          const nextSession = remainingSessions[0] ?? null;
          next.currentSession = nextSession;
          next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
          if (!deletingCurrent) {
            // 删除非当前会话时，级联删除可能移除了当前会话；这时才需要
            // 为新选中的会话触发一次历史加载。
            next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
          }
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.llmRequestLogsLoading = false;
          next.llmRequestLogsError = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionResourcesLoading = false;
          next.sessionResourcesError = null;
          next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
          Object.assign(next, resetAgentStateFields(next));
          if (nextSession) {
            writeLastSessionId(nextSession.session_id);
          } else {
            clearLastSessionId();
          }
        }

        next.status = refreshFailure
          ? `已删除会话: ${result.session_id}；会话列表刷新失败: ${refreshFailure}`
          : `已删除会话: ${result.session_id}`;
        return next;
      });
    },
    [
      abortCurrentStream,
      activeGatewayWorkspaceId,
      apiPort,
      currentSession?.session_id,
      currentSessionGatewayWorkspaceId,
      invalidateAgentState,
      setState,
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
