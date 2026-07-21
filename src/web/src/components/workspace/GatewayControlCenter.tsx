import { useCallback, useEffect, useMemo, useState } from "react";
import { getGatewayHealth } from "../../gatewayApi";
import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayHealth,
  GatewayRuntimeBlocker,
  GatewayRuntimeRestartResult,
  GatewayWorkspace,
} from "../../types/backend";
import GatewayInboundAccessPanel from "./GatewayInboundAccessPanel";
import GatewayRoutingOverview from "./GatewayRoutingOverview";
import WorkspaceLocalDialog from "./WorkspaceLocalDialog";
import WorkspaceRenameDialog from "./WorkspaceRenameDialog";
import WorkspaceSshDialog from "./WorkspaceSshDialog";
import { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";

export { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";

interface GatewayControlCenterProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  recentLocalWorkspacePaths: string[];
  switching: boolean;
  removingWorkspaceIds: Set<string>;
  gatewayError: string | null;
  onActivate: (workspaceId: string) => Promise<void>;
  onAddLocal: (payload: AddLocalGatewayWorkspaceRequest) => Promise<void>;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
  onRemove: (workspaceId: string, workspaceName: string) => void;
  onReorder: (workspaceIds: string[]) => Promise<void>;
  onRefresh: () => Promise<void>;
  onReconnect: (workspaceId: string) => Promise<void>;
  onSafeRestartManagedBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  onForceRestartManagedBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  onProbeExternalBackend: (workspaceId: string) => Promise<void>;
  onRename: (workspaceId: string, name: string) => Promise<string>;
  onUseWorkspace: (workspaceId: string) => Promise<void>;
}

type GatewayConsoleView = "routing" | "managed";

export default function GatewayControlCenter({
  apiPort,
  workspaces,
  activeWorkspaceId,
  recentLocalWorkspacePaths,
  switching,
  removingWorkspaceIds,
  gatewayError,
  onActivate,
  onAddLocal,
  onAddSsh,
  onRemove,
  onReorder,
  onRefresh,
  onReconnect,
  onSafeRestartManagedBackend,
  onForceRestartManagedBackend,
  onProbeExternalBackend,
  onRename,
  onUseWorkspace,
}: GatewayControlCenterProps) {
  const [localDialogOpen, setLocalDialogOpen] = useState(false);
  const [sshDialogOpen, setSshDialogOpen] = useState(false);
  const [consoleView, setConsoleView] = useState<GatewayConsoleView>("routing");
  const [renamingWorkspace, setRenamingWorkspace] =
    useState<GatewayWorkspace | null>(null);
  const [health, setHealth] = useState<GatewayHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busyWorkspaceId, setBusyWorkspaceId] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [restartBlockers, setRestartBlockers] = useState<GatewayRuntimeBlocker[]>([]);
  const [restartMode, setRestartMode] = useState<"safe" | "force" | null>(null);
  const [reconnectingWorkspaceId, setReconnectingWorkspaceId] =
    useState<string | null>(null);

  const loadHealth = useCallback(async () => {
    try {
      const nextHealth = await getGatewayHealth(apiPort);
      setHealth(nextHealth);
      setHealthError(null);
    } catch (error) {
      setHealth(null);
      setHealthError(error instanceof Error ? error.message : String(error));
    }
  }, [apiPort]);

  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  const stats = useMemo(() => {
    const ready = workspaces.filter(
      (workspace) => workspace.status === "ready",
    ).length;
    const remoteGatewayConnections = new Set(
      workspaces.flatMap((workspace) =>
        workspace.connection_kind === "remote_gateway" && workspace.remote
          ? [workspace.remote.gateway_connection_id]
          : [],
      ),
    ).size;
    const localWorkspaces = workspaces.filter(
      (workspace) => workspace.connection_kind === "local",
    ).length;
    const serviceStates = workspaces.flatMap((workspace) =>
      Object.values(workspace.services).map((service) => service.status),
    );
    return {
      ready,
      offline: workspaces.length - ready,
      remoteGatewayConnections,
      localWorkspaces,
      serviceReady: serviceStates.filter((status) => status === "ready").length,
      serviceProblems: serviceStates.filter((status) => status !== "ready").length,
    };
  }, [workspaces]);

  const checkedAt = useMemo(() => {
    const timestamps = workspaces
      .map((workspace) => Date.parse(workspace.checked_at))
      .filter(Number.isFinite);
    return timestamps.length > 0 ? new Date(Math.max(...timestamps)) : null;
  }, [workspaces]);

  const workspaceGroups = useMemo(
    () => groupGatewayWorkspaces(workspaces),
    [workspaces],
  );
  const activeWorkspace = workspaces.find(
    (workspace) => workspace.workspace_id === activeWorkspaceId,
  );
  const gatewayAvailable = health?.status === "ok";
  const gatewayRuntimeContractOutdated = workspaces.some(
    (workspace) => !workspace.runtime_action,
  );

  const handleRefresh = async () => {
    setRefreshing(true);
    setOperationError(null);
    setOperationNotice(null);
    try {
      await Promise.all([onRefresh(), loadHealth()]);
      setOperationNotice("Gateway 状态和全部工作区服务已重新探测。");
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    } finally {
      setRefreshing(false);
    }
  };

  const runWorkspaceAction = async (
    workspaceId: string,
    action: () => Promise<unknown>,
  ): Promise<boolean> => {
    setBusyWorkspaceId(workspaceId);
    setOperationError(null);
    setOperationNotice(null);
    try {
      await action();
      await loadHealth();
      return true;
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      setBusyWorkspaceId(null);
    }
  };

  const handleRuntimeAction = async (workspace: GatewayWorkspace) => {
    if (!workspace.runtime_action) {
      setOperationError(
        "当前 Gateway 版本未声明运行时控制能力，请重启 Gateway 后重试。",
      );
      return;
    }
    let action: () => Promise<void>;
    let completedNotice: string;
    if (workspace.runtime_action === "reconnect_remote_gateway") {
      const confirmed = window.confirm(
        `重新连接「${workspace.name}」所属的远程 Gateway？\n\n该操作只重建单个 Gateway SSH 隧道，不会直接连接或重启 Workspace 后端。正在使用该远程 Gateway 的请求会短暂中断。`,
      );
      if (!confirmed) {
        return;
      }
      action = () => onReconnect(workspace.workspace_id);
      completedNotice = `「${workspace.name}」所属的远程 Gateway 已重新连接。`;
    } else {
      action = () => onProbeExternalBackend(workspace.workspace_id);
      completedNotice = `「${workspace.name}」外部后端已重新探测。`;
    }
    setReconnectingWorkspaceId(workspace.workspace_id);
    const succeeded = await runWorkspaceAction(workspace.workspace_id, action);
    setReconnectingWorkspaceId(null);
    if (succeeded) {
      setOperationNotice(completedNotice);
    }
  };

  const handleRemoteGatewayReconnect = async (workspace: GatewayWorkspace) => {
    if (workspace.connection_kind !== "remote_gateway" || !workspace.remote) {
      setOperationError(`工作区 ${workspace.workspace_id} 不属于远程 Gateway`);
      return;
    }
    const group = workspaceGroups.find(
      (item) =>
        item.key === `remote:${workspace.remote?.gateway_connection_id}`,
    );
    const confirmed = window.confirm(
      `重新连接「${workspace.remote.ssh_config_host ?? workspace.remote.host}」？\n\n该连接下的 ${group?.workspaces.length ?? 1} 个工作区会短暂中断。`,
    );
    if (!confirmed) {
      return;
    }
    setReconnectingWorkspaceId(workspace.workspace_id);
    const succeeded = await runWorkspaceAction(
      workspace.workspace_id,
      () => onReconnect(workspace.workspace_id),
    );
    setReconnectingWorkspaceId(null);
    if (succeeded) {
      setOperationNotice(
        `远程 Gateway「${workspace.remote.ssh_config_host ?? workspace.remote.host}」已重新连接。`,
      );
    }
  };

  const handleSafeRestart = async (workspace: GatewayWorkspace) => {
    const confirmed = window.confirm(
      `安全重启「${workspace.name}」的 Workspace 后端？\n\nGateway 会先停止接收新任务，最多等待 30 秒让 Agent、工具和后台任务结束。若仍有活动任务，将取消本次重启并恢复服务。Terminal 和 Browser 服务不会重启。`,
    );
    if (!confirmed) {
      return;
    }
    setBusyWorkspaceId(workspace.workspace_id);
    setRestartMode("safe");
    setRestartBlockers([]);
    setOperationError(null);
    setOperationNotice(null);
    try {
      const result = await onSafeRestartManagedBackend(workspace.workspace_id);
      setRestartBlockers(result.blockers);
      setOperationNotice(
        result.status === "restarted"
          ? `「${workspace.name}」Workspace 后端已安全重启。`
          : `「${workspace.name}」仍有活动任务，安全重启已取消。`,
      );
      await loadHealth();
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    } finally {
      setRestartMode(null);
      setBusyWorkspaceId(null);
    }
  };

  const handleForceRestart = async (workspace: GatewayWorkspace) => {
    const confirmed = window.confirm(
      `强制重启「${workspace.name}」的 Workspace 后端？\n\n正在运行的 Agent、工具和后台任务会被明确标记为中断并取消，可能留下尚未完成的外部副作用。`,
    );
    if (!confirmed) {
      return;
    }
    setBusyWorkspaceId(workspace.workspace_id);
    setRestartMode("force");
    setRestartBlockers([]);
    setOperationError(null);
    setOperationNotice(null);
    try {
      const result = await onForceRestartManagedBackend(workspace.workspace_id);
      setRestartBlockers(result.blockers);
      setOperationNotice(
        `「${workspace.name}」Workspace 后端已强制重启，${result.blockers.length} 个活动资源已记录为中断。`,
      );
      await loadHealth();
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    } finally {
      setRestartMode(null);
      setBusyWorkspaceId(null);
    }
  };

  const handleRename = async (workspaceId: string, name: string) => {
    setOperationError(null);
    setOperationNotice(null);
    const authoritativeName = await onRename(workspaceId, name);
    setOperationNotice(`工作区已重命名为「${authoritativeName}」。`);
    return authoritativeName;
  };

  const moveWorkspace = async (
    workspaceId: string,
    siblingIds: string[],
    direction: -1 | 1,
  ) => {
    const siblingIndex = siblingIds.indexOf(workspaceId);
    const targetSiblingIndex = siblingIndex + direction;
    if (
      siblingIndex < 0 ||
      targetSiblingIndex < 0 ||
      targetSiblingIndex >= siblingIds.length
    ) {
      return;
    }
    const workspaceIds = workspaces.map((workspace) => workspace.workspace_id);
    const index = workspaceIds.indexOf(workspaceId);
    const targetIndex = workspaceIds.indexOf(siblingIds[targetSiblingIndex]);
    [workspaceIds[index], workspaceIds[targetIndex]] = [
      workspaceIds[targetIndex],
      workspaceIds[index],
    ];
    setOperationError(null);
    try {
      await onReorder(workspaceIds);
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <main className="gateway-control-shell">
      <aside className="gateway-control-sidebar">
        <div className="gateway-control-brand">
          <span className="codicon codicon-server-process" aria-hidden="true" />
          <div><strong>BoxTeam</strong><small>Local Control UI</small></div>
        </div>
        <nav aria-label="Gateway 控制面导航">
          <section>
            <p>控制面</p>
            <button type="button" disabled>
              <span className="codicon codicon-dashboard" aria-hidden="true" />概览
            </button>
            <button
              type="button"
              className={consoleView === "routing" ? "active" : undefined}
              onClick={() => setConsoleView("routing")}
            >
              <span className="codicon codicon-git-merge" aria-hidden="true" />
              工作区与路由
              <small>{workspaces.length}</small>
            </button>
            <button
              type="button"
              className={consoleView === "managed" ? "active" : undefined}
              onClick={() => setConsoleView("managed")}
            >
              <span className="codicon codicon-remote" aria-hidden="true" />
              外部接入
            </button>
          </section>
          <section>
            <p>系统</p>
            <button type="button" disabled>
              <span className="codicon codicon-pulse" aria-hidden="true" />服务运行时
            </button>
            <button type="button" disabled>
              <span className="codicon codicon-shield" aria-hidden="true" />连接与凭据
            </button>
            <button type="button" disabled>
              <span className="codicon codicon-output" aria-hidden="true" />日志与诊断
            </button>
          </section>
        </nav>
        <div className="gateway-control-sidebar-status">
          <strong><span />{gatewayAvailable ? "Gateway 正常" : "Gateway 异常"}</strong>
          <code>127.0.0.1:{apiPort}</code>
          <small>{checkedAt ? `检查于 ${checkedAt.toLocaleTimeString()}` : "等待首次探测"}</small>
        </div>
      </aside>

      <section className="gateway-control-main" aria-labelledby="gateway-control-title">
        <div className="gateway-control-content">
          <header className="gateway-control-header">
            <div>
              <span>本地控制面</span>
              <h1 id="gateway-control-title">
                {consoleView === "routing" ? "工作区与路由" : "外部 Gateway 接入"}
              </h1>
              <p>
                {consoleView === "routing"
                  ? "查看本机工作区、远程 Gateway 投影与当前请求路由。当前 Gateway 负责选择目标和透明转发。"
                  : "查看哪些其他 Gateway 已接入本机，并能访问本机直接工作区。"}
              </p>
            </div>
            <div className="gateway-control-actions">
              <button type="button" onClick={() => setLocalDialogOpen(true)}>
                <span className="codicon codicon-folder-opened" aria-hidden="true" />
                添加本机工作区
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => setSshDialogOpen(true)}
              >
                <span className="codicon codicon-remote" aria-hidden="true" />
                连接远程 Gateway
              </button>
              <button
                type="button"
                className="icon-only"
                aria-label="刷新 Gateway"
                title="刷新 Gateway"
                disabled={refreshing}
                onClick={() => void handleRefresh()}
              >
                <span
                  className={`codicon codicon-refresh${
                    refreshing ? " codicon-modifier-spin" : ""
                  }`}
                  aria-hidden="true"
                />
              </button>
            </div>
          </header>

          {gatewayError || healthError || operationError ? (
            <div className="gateway-console-alert" role="alert">
              <span className="codicon codicon-error" aria-hidden="true" />
              <div><strong>Gateway 操作失败</strong><span>{gatewayError ?? healthError ?? operationError}</span></div>
            </div>
          ) : null}
          {operationNotice ? (
            <div className="gateway-console-notice" role="status">
              <span className="codicon codicon-pass-filled" aria-hidden="true" />
              {operationNotice}
            </div>
          ) : null}
          {restartBlockers.length > 0 ? (
            <div className="gateway-console-blockers" role="status">
              <span className="codicon codicon-warning" aria-hidden="true" />
              <div>
                <strong>重启涉及的活动资源</strong>
                <ul>
                  {restartBlockers.map((blocker) => (
                    <li key={`${blocker.kind}:${blocker.resource_id}`}>
                      <code>{blocker.kind}</code>
                      <span>{blocker.detail ?? blocker.resource_id}</span>
                      <small>会话 {blocker.session_id} · {blocker.status}</small>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ) : null}
          {gatewayRuntimeContractOutdated ? (
            <div className="gateway-console-version-notice" role="status">
              <span className="codicon codicon-warning" aria-hidden="true" />
              当前 Gateway 进程版本较旧，运行时控制暂不可用；重启 Gateway 后启用。
            </div>
          ) : null}

          <section className="gateway-overview-strip" aria-label="Gateway 状态摘要">
            <div className="gateway-overview-primary">
              <span className="codicon codicon-pulse" aria-hidden="true" />
              <div>
                <strong>
                  {gatewayAvailable
                    ? stats.offline === 0 && stats.serviceProblems === 0
                      ? "控制面在线，所有关键链路正常"
                      : "控制面在线，部分工作区需要处理"
                    : "控制面状态异常"}
                </strong>
                <small>
                  {checkedAt
                    ? `最后探测 ${checkedAt.toLocaleTimeString()}`
                    : "正在等待工作区探测"}
                </small>
              </div>
            </div>
            <article><span>工作区</span><strong>{workspaces.length}</strong><small>{stats.localWorkspaces} 本机 · {workspaces.length - stats.localWorkspaces} 远程</small></article>
            <article><span>远程 Gateway</span><strong>{stats.remoteGatewayConnections}</strong><small>{stats.remoteGatewayConnections > 0 ? "SSH 隧道已连接" : "尚未连接"}</small></article>
            <article><span>服务端点</span><strong>{stats.serviceReady}</strong><small>{stats.serviceProblems > 0 ? `${stats.serviceProblems} 个异常` : "全部探测正常"}</small></article>
          </section>

          <div className="gateway-view-tabs" role="tablist" aria-label="Gateway 控制面视图">
            <button
              type="button"
              role="tab"
              aria-selected={consoleView === "routing"}
              className={consoleView === "routing" ? "active" : undefined}
              onClick={() => setConsoleView("routing")}
            >
              路由总览
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={consoleView === "managed"}
              className={consoleView === "managed" ? "active" : undefined}
              onClick={() => setConsoleView("managed")}
            >
              外部接入
            </button>
          </div>

          {consoleView === "routing" ? (
            <GatewayRoutingOverview
              groups={workspaceGroups}
              activeWorkspace={activeWorkspace}
              activeWorkspaceId={activeWorkspaceId}
              checkedAt={checkedAt}
              gatewayAvailable={gatewayAvailable}
              switching={switching}
              busyWorkspaceId={busyWorkspaceId}
              removingWorkspaceIds={removingWorkspaceIds}
              reconnectingWorkspaceId={reconnectingWorkspaceId}
              restartMode={restartMode}
              onActivate={async (workspace) => {
                await runWorkspaceAction(
                  workspace.workspace_id,
                  () => onActivate(workspace.workspace_id),
                );
              }}
              onUseWorkspace={async (workspace) => {
                await runWorkspaceAction(
                  workspace.workspace_id,
                  () => onUseWorkspace(workspace.workspace_id),
                );
              }}
              onRename={setRenamingWorkspace}
              onMove={moveWorkspace}
              onRuntimeAction={handleRuntimeAction}
              onSafeRestart={handleSafeRestart}
              onForceRestart={handleForceRestart}
              onReconnectGateway={handleRemoteGatewayReconnect}
              onRemove={onRemove}
            />
          ) : (
            <GatewayInboundAccessPanel apiPort={apiPort} />
          )}
        </div>
      </section>

      <WorkspaceLocalDialog
        open={localDialogOpen}
        apiPort={apiPort}
        workspaces={workspaces}
        activeWorkspaceId={activeWorkspaceId}
        recentLocalWorkspacePaths={recentLocalWorkspacePaths}
        onClose={() => setLocalDialogOpen(false)}
        onSubmit={onAddLocal}
      />
      <WorkspaceSshDialog
        open={sshDialogOpen}
        apiPort={apiPort}
        onClose={() => setSshDialogOpen(false)}
        onSubmit={onAddSsh}
      />
      <WorkspaceRenameDialog
        workspace={renamingWorkspace}
        onClose={() => setRenamingWorkspace(null)}
        onSubmit={handleRename}
      />
    </main>
  );
}
