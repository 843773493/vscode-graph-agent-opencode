import {
  getWorkspace,
  getSession,
  listAgents as apiListAgents,
} from "../../api";
import { HttpRequestError } from "../../api/http";
import {
  activateGatewayWorkspace,
  getGatewayUiSettings,
  listGatewayWorkspaces,
} from "../../gatewayApi";
import { ensureGatewayUserAccess } from "../../api/gateway/userAccess";
import { getLatestGatewayUserViewState } from "../../api/gateway/userViewState";
import type { AppState } from "../../types/frontend";
import { loadAndApplyResolvedGatewayTheme } from "../../theme/theme";
import {
  fetchWorkspaceSessionListSnapshot,
  type WorkspaceSessionListSnapshot,
} from "./workspaceSessionListRefresh";
import { requireGatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import {
  isRetryableWorkspaceBootstrapError,
  selectBootstrapSessionId,
  selectHealthyGatewayWorkspace,
  shouldRestorePersistedWorkspace,
  type BootstrapSelection,
  type GatewayWorkspaceList,
} from "./workspaceBootstrapPolicy";

type WorkspaceBootstrapPayload = {
  selectedWorkspaceId: string;
  userAccess: Awaited<ReturnType<typeof ensureGatewayUserAccess>>;
  userViewState: Awaited<ReturnType<typeof getLatestGatewayUserViewState>>;
  gatewayWorkspaces: Awaited<ReturnType<typeof listGatewayWorkspaces>>;
  uiSettings: Awaited<ReturnType<typeof getGatewayUiSettings>>;
  /** 主题背景图加载失败的可见警告；核心主题已应用，非致命。undefined 表示本轮
   * 复用了当前 UI 设置、没有重新加载主题，调用方必须保留原有警告而不是清空。 */
  themeBackgroundWarning?: string | null;
  workspace: Awaited<ReturnType<typeof getWorkspace>>;
  workspaceSessionResults: PromiseSettledResult<WorkspaceSessionListSnapshot>[];
  agents: Awaited<ReturnType<typeof apiListAgents>>;
};

export class WorkspaceBootstrapUnavailableError extends Error {
  readonly gatewayWorkspaces: GatewayWorkspaceList;

  constructor(gatewayWorkspaces: GatewayWorkspaceList) {
    const activeWorkspace = gatewayWorkspaces.items.find(
      (workspace) => workspace.workspace_id === gatewayWorkspaces.active_workspace_id,
    );
    const activeWorkspaceDescription = activeWorkspace
      ? `活动工作区“${activeWorkspace.name}”当前不可用`
      : "当前没有活动工作区";
    super(
      `${activeWorkspaceDescription}，当前没有可用工作区。请在工作区列表中启动或重连工作区后重试。`,
    );
    this.name = "WorkspaceBootstrapUnavailableError";
    this.gatewayWorkspaces = gatewayWorkspaces;
  }
}

const BOOTSTRAP_RETRY_DELAYS_MS = [250, 500, 1000, 2000, 3000, 5000, 5000];

/** 初始化重试等待：必须可被外部 signal 立即打断。
 *
 * 以前这里只 sleep 完再 throwIfAborted，等待本身不可取消；调用方用缺省 signal
 * 兜底时更是完全不可中断（最长 16.25 秒卡住工作区切换或卸载流程）。现在等待
 * 期间监听 abort，一旦中止立刻结束等待并由 throwIfAborted 抛出。 */
export async function waitForBootstrapRetry(
  delayMs: number,
  signal: AbortSignal,
): Promise<void> {
  await new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const onAbort = () => {
      globalThis.clearTimeout(timerId);
      resolve();
    };
    const timerId = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
  signal.throwIfAborted();
}

async function loadWorkspaceBootstrap(
  apiPort: number,
  options: {
    fixedSelection?: BootstrapSelection | null;
    restorePersistedWorkspace: boolean;
    checkGatewayWorkspaceHealth: boolean;
    preferredSessionId?: string | null;
    previousUserAccess: AppState["gatewayUserAccess"];
    uiSettings?: Awaited<ReturnType<typeof getGatewayUiSettings>>;
    signal?: AbortSignal;
    assertCurrentSelection: () => void;
  },
): Promise<WorkspaceBootstrapPayload> {
  const { signal } = options;
  options.assertCurrentSelection();
  const userAccess = await ensureGatewayUserAccess(apiPort);
  signal?.throwIfAborted();
  options.assertCurrentSelection();
  const uiSettingsPromise = options.uiSettings
    ? Promise.resolve(options.uiSettings)
    : getGatewayUiSettings(apiPort);
  const gatewayWorkspacesPromise = listGatewayWorkspaces(apiPort, {
    checkHealth: options.checkGatewayWorkspaceHealth,
  });
  const [uiSettings, initialGatewayWorkspaces] = await Promise.all([
    uiSettingsPromise,
    gatewayWorkspacesPromise,
  ]);
  signal?.throwIfAborted();
  options.assertCurrentSelection();
  const userViewStatePromise = userAccess.kind === "user"
    ? getLatestGatewayUserViewState(apiPort)
    : Promise.resolve(null);
  const userViewState = await userViewStatePromise;
  signal?.throwIfAborted();
  options.assertCurrentSelection();
  if (!uiSettings.theme.resolved_theme) {
    throw new Error("Gateway UI Settings 缺少已解析主题");
  }
  // 主题应用分两段：核心 token/配色是必需的，失败即让整个引导失败；背景图
  // 只是装饰画布，加载失败降级为可见警告，绝不能把工作区初始化一起拖垮。
  const themeBackgroundWarning = options.uiSettings
    ? undefined
    : (await loadAndApplyResolvedGatewayTheme(uiSettings.theme.resolved_theme)).backgroundWarning;
  let gatewayWorkspaces = initialGatewayWorkspaces;
  const persistedWorkspaceId = userViewState?.workspace_id;
  options.assertCurrentSelection();
  if (
    shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: options.restorePersistedWorkspace && !options.fixedSelection,
      userViewState,
      activeWorkspaceId: gatewayWorkspaces.active_workspace_id,
      availableWorkspaceIds: gatewayWorkspaces.items.map(
        (workspace) => workspace.workspace_id,
      ),
    })
    && persistedWorkspaceId
  ) {
    await activateGatewayWorkspace(apiPort, persistedWorkspaceId, signal);
    signal?.throwIfAborted();
    options.assertCurrentSelection();
    gatewayWorkspaces = await listGatewayWorkspaces(apiPort, {
      checkHealth: options.checkGatewayWorkspaceHealth,
    });
  }
  signal?.throwIfAborted();
  options.assertCurrentSelection();
  // 扩展窗口固定 URL owner，只读取该目标；不恢复其它会话或改变 Gateway 激活状态。
  let selectedWorkspaceId: string;
  const fixedSelection = options.fixedSelection;
  if (fixedSelection) {
    const requested = gatewayWorkspaces.items.find(
      (entry) => entry.workspace_id === fixedSelection.workspaceId,
    );
    if (requested?.status !== "ready") {
      throw new Error(`扩展窗口目标工作区不可用: ${fixedSelection.workspaceId}`);
    }
    selectedWorkspaceId = requested.workspace_id;
  } else {
    // 不检查健康的列表可带重启前的 offline 快照；改变目标前必须核对真实状态。
    let healthyWorkspaceId = selectHealthyGatewayWorkspace(gatewayWorkspaces);
    if (!options.checkGatewayWorkspaceHealth
      && (healthyWorkspaceId === null
        || healthyWorkspaceId !== gatewayWorkspaces.active_workspace_id)) {
      gatewayWorkspaces = await listGatewayWorkspaces(apiPort, { checkHealth: true });
      signal?.throwIfAborted();
      options.assertCurrentSelection();
      healthyWorkspaceId = selectHealthyGatewayWorkspace(gatewayWorkspaces);
    }
    if (healthyWorkspaceId === null) {
      throw new WorkspaceBootstrapUnavailableError(gatewayWorkspaces);
    }
    if (healthyWorkspaceId !== gatewayWorkspaces.active_workspace_id) {
      await activateGatewayWorkspace(apiPort, healthyWorkspaceId, signal);
      signal?.throwIfAborted();
      options.assertCurrentSelection();
      gatewayWorkspaces = await listGatewayWorkspaces(apiPort, {
        checkHealth: options.checkGatewayWorkspaceHealth,
      });
      healthyWorkspaceId = selectHealthyGatewayWorkspace(gatewayWorkspaces);
    }
    const activeWorkspaceId = gatewayWorkspaces.active_workspace_id;
    if (!activeWorkspaceId || healthyWorkspaceId !== activeWorkspaceId) {
      throw new WorkspaceBootstrapUnavailableError(gatewayWorkspaces);
    }
    selectedWorkspaceId = activeWorkspaceId;
  }
  const [workspace, workspaceSessionResults, agents] = await Promise.all([
    getWorkspace(apiPort, selectedWorkspaceId),
    Promise.allSettled(
      selectedWorkspaceId
        ? [fetchWorkspaceSessionListSnapshot(
            apiPort,
            requireGatewayWorkspaceRequestTarget(
              gatewayWorkspaces.items,
              selectedWorkspaceId,
            ),
          )]
        : [],
    ),
    apiListAgents(apiPort, selectedWorkspaceId),
  ]);
  const targetSessionId = options.fixedSelection?.sessionId ?? selectBootstrapSessionId({
    preferredSessionId: options.preferredSessionId,
    persistedSessionId: userViewState?.workspace_id === selectedWorkspaceId ? userViewState.session_id : null,
    userChanged: options.previousUserAccess?.kind !== userAccess.kind
      || options.previousUserAccess?.user_id !== userAccess.user_id,
  });
  const sessionResult = workspaceSessionResults[0];
  if (fixedSelection && sessionResult?.status === "rejected") throw sessionResult.reason;
  if (targetSessionId && sessionResult?.status === "fulfilled"
    && (options.fixedSelection || (!sessionResult.value.complete
      && !sessionResult.value.sessions.some((session) => session.session_id === targetSessionId)))) {
    // 恢复首页外的选择只定点读取目标，不能为了定位会话拉完全部分页。
    try {
      const selected = await getSession(apiPort, targetSessionId, selectedWorkspaceId, { signal });
      sessionResult.value = { ...sessionResult.value, sessions: [
        ...sessionResult.value.sessions.filter((entry) => entry.session_id !== selected.session_id), selected,
      ] };
    } catch (error: unknown) {
      if (options.fixedSelection || !(error instanceof HttpRequestError) || error.status !== 404) throw error;
    }
  }
  return {
    selectedWorkspaceId,
    userAccess,
    userViewState,
    gatewayWorkspaces,
    uiSettings,
    themeBackgroundWarning,
    workspace,
    workspaceSessionResults,
    agents,
  };
}

export async function loadWorkspaceBootstrapWithRetry(
  apiPort: number,
  // signal 必须是必填：重试等待只有在始终持有可取消 signal 时才谈得上可中断，
  // 不允许再用「缺省 signal」兜底出一个不可取消的等待。
  options: Parameters<typeof loadWorkspaceBootstrap>[1] & { signal: AbortSignal },
): Promise<WorkspaceBootstrapPayload> {
  const { signal } = options;
  for (let attempt = 0; ; attempt += 1) {
    signal.throwIfAborted();
    options.assertCurrentSelection();
    try {
      return await loadWorkspaceBootstrap(apiPort, options);
    } catch (error: unknown) {
      if (
        !isRetryableWorkspaceBootstrapError(error)
        || attempt >= BOOTSTRAP_RETRY_DELAYS_MS.length
      ) {
        throw error;
      }
      await waitForBootstrapRetry(BOOTSTRAP_RETRY_DELAYS_MS[attempt], signal);
    }
  }
}
