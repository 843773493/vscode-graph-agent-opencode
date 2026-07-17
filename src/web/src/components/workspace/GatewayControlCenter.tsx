import { useCallback, useEffect, useMemo, useState } from "react";
import { getGatewayHealth } from "../../gatewayApi";
import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayHealth,
  GatewayServiceStatus,
  GatewayWorkspace,
} from "../../types/backend";
import WorkspaceLocalDialog from "./WorkspaceLocalDialog";
import WorkspaceRenameDialog from "./WorkspaceRenameDialog";
import WorkspaceSshDialog from "./WorkspaceSshDialog";

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
  onRename: (workspaceId: string, name: string) => Promise<string>;
  onUseWorkspace: (workspaceId: string) => Promise<void>;
}

function workspaceKindLabel(workspace: GatewayWorkspace): string {
  if (workspace.connection_kind === "local") {
    return workspace.managed ? "本机 · Gateway 托管" : "本机 · 外部后端";
  }
  return "SSH 远程";
}

function serviceEntries(workspace: GatewayWorkspace) {
  const order = ["workspace_api", "terminal_manager", "browser_manager"];
  return Object.entries(workspace.services).sort(
    ([left], [right]) => order.indexOf(left) - order.indexOf(right),
  );
}

function serviceLabel(name: string): string {
  const labels: Record<string, string> = {
    workspace_api: "Workspace API",
    terminal_api: "Terminal API",
    terminal_ui: "Terminal UI",
    browser_api: "Browser API",
    browser_ui: "Browser UI",
  };
  return labels[name] ?? name;
}

function serviceRouteLabel(service: GatewayServiceStatus): string {
  const local = service.local_port != null ? `:${service.local_port}` : null;
  const remote = service.remote_port != null
    ? `${service.remote_host ?? "remote"}:${service.remote_port}`
    : null;
  if (local && remote) {
    return `${local} → ${remote}`;
  }
  return local ?? remote ?? "未分配端口";
}

function serviceStatusLabel(status: GatewayServiceStatus["status"]): string {
  const labels: Record<GatewayServiceStatus["status"], string> = {
    ready: "正常",
    offline: "离线",
    unavailable: "未提供",
  };
  return labels[status];
}

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
  onRename,
  onUseWorkspace,
}: GatewayControlCenterProps) {
  const [localDialogOpen, setLocalDialogOpen] = useState(false);
  const [sshDialogOpen, setSshDialogOpen] = useState(false);
  const [renamingWorkspace, setRenamingWorkspace] = useState<GatewayWorkspace | null>(null);
  const [health, setHealth] = useState<GatewayHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busyWorkspaceId, setBusyWorkspaceId] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [reconnectingWorkspaceId, setReconnectingWorkspaceId] = useState<string | null>(null);

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
    const ready = workspaces.filter((workspace) => workspace.status === "ready").length;
    const offline = workspaces.length - ready;
    const local = workspaces.filter((workspace) => workspace.connection_kind === "local").length;
    const ssh = workspaces.length - local;
    const serviceStates = workspaces.flatMap((workspace) =>
      Object.values(workspace.services).map((service) => service.status),
    );
    const serviceReady = serviceStates.filter((status) => status === "ready").length;
    const serviceOffline = serviceStates.filter((status) => status === "offline").length;
    const serviceUnavailable = serviceStates.filter((status) => status === "unavailable").length;
    return { ready, offline, local, ssh, serviceReady, serviceOffline, serviceUnavailable };
  }, [workspaces]);

  const checkedAt = useMemo(() => {
    const timestamps = workspaces
      .map((workspace) => Date.parse(workspace.checked_at))
      .filter(Number.isFinite);
    return timestamps.length > 0 ? new Date(Math.max(...timestamps)) : null;
  }, [workspaces]);

  const activeWorkspace = workspaces.find(
    (workspace) => workspace.workspace_id === activeWorkspaceId,
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
    action: () => Promise<void>,
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

  const handleReconnect = async (workspace: GatewayWorkspace) => {
    const confirmed = window.confirm(
      `重新连接「${workspace.name}」？\n\n该操作会关闭现有的 Gateway 托管进程或 SSH 隧道，然后按保存的连接信息重新建立服务。正在使用这些连接的终端、浏览器或请求可能会短暂中断。`,
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
      setOperationNotice(`「${workspace.name}」重连流程已结束，服务状态已重新探测。`);
    }
  };

  const handleRename = async (workspaceId: string, name: string) => {
    setOperationError(null);
    setOperationNotice(null);
    const authoritativeName = await onRename(workspaceId, name);
    setOperationNotice(`工作区已重命名为「${authoritativeName}」。`);
    return authoritativeName;
  };

  const moveWorkspace = async (index: number, direction: -1 | 1) => {
    const targetIndex = index + direction;
    if (targetIndex < 0 || targetIndex >= workspaces.length) {
      return;
    }
    const workspaceIds = workspaces.map((workspace) => workspace.workspace_id);
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

  const gatewayAvailable = health?.status === "ok";

  return (
    <main className="gateway-console-host">
      <section className="gateway-console" aria-labelledby="gateway-console-title">
        <header className="gateway-console-header">
          <div>
            <div className="gateway-console-eyebrow">本地控制面</div>
            <h1 id="gateway-console-title">Gateway 控制台</h1>
            <p>管理工作区后端、SSH 连接和浏览器请求的活动路由目标。</p>
          </div>
          <div className="gateway-console-header-actions">
            <span className={`gateway-health-pill ${gatewayAvailable ? "ready" : "offline"}`}>
              <span className="codicon codicon-pulse" aria-hidden="true" />
              {gatewayAvailable ? "Gateway 正常" : "Gateway 状态异常"}
            </span>
            <button type="button" onClick={() => setLocalDialogOpen(true)}>
              <span className="codicon codicon-folder-opened" aria-hidden="true" />
              添加本机工作区
            </button>
            <button type="button" onClick={() => setSshDialogOpen(true)}>
              <span className="codicon codicon-remote" aria-hidden="true" />
              添加 SSH 工作区
            </button>
            <button type="button" onClick={() => void handleRefresh()} disabled={refreshing}>
              <span className={`codicon codicon-refresh${refreshing ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
              {refreshing ? "刷新中" : "刷新"}
            </button>
          </div>
        </header>

        {gatewayError || healthError || operationError ? (
          <div className="gateway-console-alert" role="alert">
            <span className="codicon codicon-error" aria-hidden="true" />
            <div>
              <strong>Gateway 状态读取失败</strong>
              <span>{gatewayError ?? healthError ?? operationError}</span>
            </div>
          </div>
        ) : null}

        {operationNotice ? (
          <div className="gateway-console-notice" role="status">
            <span className="codicon codicon-pass-filled" aria-hidden="true" />
            {operationNotice}
          </div>
        ) : null}

        <div className="gateway-summary-grid" aria-label="Gateway 状态摘要">
          <article>
            <span>已注册工作区</span>
            <strong>{workspaces.length}</strong>
            <small>{activeWorkspace ? `当前：${activeWorkspace.name}` : "尚未激活工作区"}</small>
          </article>
          <article>
            <span>Workspace API</span>
            <strong>{stats.ready}</strong>
            <small>{stats.offline > 0 ? `${stats.offline} 个离线` : "全部 API 已就绪"}</small>
          </article>
          <article>
            <span>服务探测</span>
            <strong>{stats.serviceReady}</strong>
            <small>
              {stats.serviceOffline > 0 || stats.serviceUnavailable > 0
                ? `${stats.serviceOffline} 离线 · ${stats.serviceUnavailable} 未提供`
                : "全部探测正常"}
            </small>
          </article>
          <article>
            <span>连接类型</span>
            <strong>{stats.local + stats.ssh}</strong>
            <small>{stats.local} 本机 · {stats.ssh} SSH</small>
          </article>
        </div>

        <div className="gateway-console-grid">
          <section className="gateway-workspace-section" aria-labelledby="gateway-workspaces-title">
            <div className="gateway-section-heading">
              <div>
                <h2 id="gateway-workspaces-title">工作区路由</h2>
                <p>活动工作区接收未指定工作区 ID 的同源 `/api/v1/*` 请求。</p>
              </div>
              <span>
                {workspaces.length} 个目标
                {checkedAt ? ` · 检查于 ${checkedAt.toLocaleTimeString()}` : ""}
              </span>
            </div>

            <div className="gateway-workspace-list">
              {workspaces.length === 0 ? (
                <div className="gateway-console-empty">
                  <span className="codicon codicon-server-environment" aria-hidden="true" />
                  <strong>尚未注册工作区</strong>
                  <p>添加本机或 SSH 工作区后，Gateway 才能路由会话和工具请求。</p>
                </div>
              ) : workspaces.map((workspace, index) => {
                const active = workspace.workspace_id === activeWorkspaceId;
                const busy = busyWorkspaceId === workspace.workspace_id ||
                  removingWorkspaceIds.has(workspace.workspace_id);
                const services = serviceEntries(workspace);
                return (
                  <article
                    key={workspace.workspace_id}
                    className={`gateway-workspace-row${active ? " active" : ""}${workspace.status === "offline" ? " offline" : ""}`}
                  >
                    <div className="gateway-workspace-main">
                      <div className="gateway-workspace-title-row">
                        <span className={`gateway-workspace-status ${workspace.status}`} aria-hidden="true" />
                        <h3>{workspace.name}</h3>
                        {active ? <span className="gateway-active-badge">活动路由</span> : null}
                        {workspace.system_default ? <span className="gateway-meta-badge">系统默认</span> : null}
                      </div>
                      <div className="gateway-workspace-kind">{workspaceKindLabel(workspace)}</div>
                      <dl className="gateway-workspace-properties">
                        <div>
                          <dt>目录</dt>
                          <dd title={workspace.root_path}>{workspace.root_path}</dd>
                        </div>
                        <div>
                          <dt>后端</dt>
                          <dd title={workspace.backend_url}>{workspace.backend_url}</dd>
                        </div>
                        <div>
                          <dt>ID</dt>
                          <dd title={workspace.workspace_id}>{workspace.workspace_id}</dd>
                        </div>
                      </dl>
                      {services.length > 0 ? (
                        <div className="gateway-service-list" aria-label={`${workspace.name} 服务状态`}>
                          {services.map(([name, service]) => (
                            <details
                              key={name}
                              className={`gateway-service-detail${service.status === "ready" ? "" : " problem"}`}
                            >
                              <summary className={`gateway-service-pill ${service.status}`}>
                                <span aria-hidden="true" />
                                {serviceLabel(name)} · {serviceStatusLabel(service.status)}
                                <code>{serviceRouteLabel(service)}</code>
                                <b>{service.status === "ready" ? "查看端点" : "查看诊断"}</b>
                              </summary>
                              <div>
                                <strong>{serviceLabel(name)} 服务详情</strong>
                                {service.status !== "ready" ? (
                                  <span>{service.error ?? "该工作区没有提供此服务，或服务尚未建立连接。"}</span>
                                ) : null}
                                <dl>
                                  <div><dt>Gateway 端点</dt><dd>{service.local_url ?? "尚未建立"}</dd></div>
                                  <div><dt>本地端口</dt><dd>{service.local_port ?? "未分配"}</dd></div>
                                  <div><dt>远端目标</dt><dd>{service.remote_port != null ? `${service.remote_host ?? "remote"}:${service.remote_port}` : "本机服务"}</dd></div>
                                  <div><dt>健康检查</dt><dd>{service.health_path}</dd></div>
                                  <div><dt>探测时间</dt><dd>{new Date(workspace.checked_at).toLocaleString()}</dd></div>
                                </dl>
                              </div>
                            </details>
                          ))}
                        </div>
                      ) : null}
                      {workspace.connection_error ? (
                        <div className="gateway-workspace-error" role="status">
                          <span className="codicon codicon-warning" aria-hidden="true" />
                          {workspace.connection_error}
                        </div>
                      ) : null}
                    </div>
                    <div className="gateway-workspace-actions">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setRenamingWorkspace(workspace)}
                      >
                        <span className="codicon codicon-edit" aria-hidden="true" />
                        重命名
                      </button>
                      <button
                        type="button"
                        title="上移工作区"
                        aria-label={`上移 ${workspace.name}`}
                        disabled={index === 0 || busy}
                        onClick={() => void moveWorkspace(index, -1)}
                      >
                        <span className="codicon codicon-arrow-up" aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        title="下移工作区"
                        aria-label={`下移 ${workspace.name}`}
                        disabled={index === workspaces.length - 1 || busy}
                        onClick={() => void moveWorkspace(index, 1)}
                      >
                        <span className="codicon codicon-arrow-down" aria-hidden="true" />
                      </button>
                      {!active ? (
                        <button
                          type="button"
                          disabled={busy || switching || workspace.status !== "ready"}
                          onClick={() => void runWorkspaceAction(
                            workspace.workspace_id,
                            () => onActivate(workspace.workspace_id),
                          )}
                        >
                          设为默认路由
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="gateway-use-button"
                        disabled={busy || workspace.status !== "ready"}
                        onClick={() => void runWorkspaceAction(
                          workspace.workspace_id,
                          () => onUseWorkspace(workspace.workspace_id),
                        )}
                      >
                        {active ? "打开会话工作台" : "设为默认路由并打开"}
                      </button>
                      {(workspace.status !== "ready" || services.some(([, service]) => service.status !== "ready")) ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void handleReconnect(workspace)}
                        >
                          <span className={`codicon codicon-debug-restart${reconnectingWorkspaceId === workspace.workspace_id ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
                          {reconnectingWorkspaceId === workspace.workspace_id ? "正在重连" : "重新连接"}
                        </button>
                      ) : null}
                      {workspace.removable ? (
                        <button
                          type="button"
                          className="gateway-danger-button"
                          disabled={busy}
                          onClick={() => onRemove(workspace.workspace_id, workspace.name)}
                        >
                          {removingWorkspaceIds.has(workspace.workspace_id) ? "移除中" : "移除"}
                        </button>
                      ) : null}
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          <aside className="gateway-routing-section" aria-labelledby="gateway-routing-title">
            <div className="gateway-section-heading">
              <div>
                <h2 id="gateway-routing-title">当前路由</h2>
                <p>浏览器始终访问同源 Gateway，由控制面选择工作区后端。</p>
              </div>
            </div>
            <div className="gateway-route-flow" aria-label="Gateway 请求链路">
              <div><span className="codicon codicon-browser" aria-hidden="true" /><strong>Web UI</strong><small>同源 /api</small></div>
              <span className="codicon codicon-arrow-right" aria-hidden="true" />
              <div><span className="codicon codicon-server-process" aria-hidden="true" /><strong>Gateway</strong><small>控制面与代理</small></div>
              <span className="codicon codicon-arrow-right" aria-hidden="true" />
              <div><span className="codicon codicon-root-folder" aria-hidden="true" /><strong>{activeWorkspace?.name ?? "未选择"}</strong><small>Workspace API</small></div>
            </div>
            <dl className="gateway-route-details">
              <div><dt>控制面状态</dt><dd>{gatewayAvailable ? "正常" : "不可确认"}</dd></div>
              <div><dt>活动工作区</dt><dd>{activeWorkspace?.workspace_id ?? "无"}</dd></div>
              <div><dt>连接类型</dt><dd>{activeWorkspace ? workspaceKindLabel(activeWorkspace) : "无"}</dd></div>
              <div><dt>目标后端</dt><dd>{activeWorkspace?.backend_url ?? "无"}</dd></div>
            </dl>
            <div className="gateway-routing-note">
              <span className="codicon codicon-info" aria-hidden="true" />
              显式携带工作区 ID 的请求可路由到非活动工作区；普通页面请求使用当前活动目标。
            </div>
          </aside>
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
