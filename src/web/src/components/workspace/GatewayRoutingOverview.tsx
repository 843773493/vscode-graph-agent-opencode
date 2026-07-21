import type {
  GatewayServiceStatus,
  GatewayWorkspace,
} from "../../types/backend";
import type { GatewayWorkspaceGroup } from "./gatewayWorkspacePresentation";
import { workspaceKindLabel } from "./gatewayWorkspacePresentation";

interface GatewayRoutingOverviewProps {
  groups: GatewayWorkspaceGroup[];
  activeWorkspace: GatewayWorkspace | undefined;
  activeWorkspaceId: string | null;
  checkedAt: Date | null;
  gatewayAvailable: boolean;
  switching: boolean;
  busyWorkspaceId: string | null;
  removingWorkspaceIds: Set<string>;
  reconnectingWorkspaceId: string | null;
  restartMode: "safe" | "force" | null;
  onActivate: (workspace: GatewayWorkspace) => Promise<void>;
  onUseWorkspace: (workspace: GatewayWorkspace) => Promise<void>;
  onRename: (workspace: GatewayWorkspace) => void;
  onMove: (
    workspaceId: string,
    siblingIds: string[],
    direction: -1 | 1,
  ) => Promise<void>;
  onRuntimeAction: (workspace: GatewayWorkspace) => Promise<void>;
  onSafeRestart: (workspace: GatewayWorkspace) => Promise<void>;
  onForceRestart: (workspace: GatewayWorkspace) => Promise<void>;
  onReconnectGateway: (workspace: GatewayWorkspace) => Promise<void>;
  onRemove: (workspaceId: string, workspaceName: string) => void;
}

const SERVICE_ORDER = ["workspace_api", "terminal_manager", "browser_manager"];

function serviceLabel(name: string): string {
  const labels: Record<string, string> = {
    workspace_api: "Workspace API",
    terminal_manager: "Terminal",
    browser_manager: "Browser",
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
  return local ?? remote ?? "未分配端点";
}

function serviceEntries(workspace: GatewayWorkspace) {
  return Object.entries(workspace.services).sort(
    ([left], [right]) =>
      SERVICE_ORDER.indexOf(left) - SERVICE_ORDER.indexOf(right),
  );
}

function workspaceRemoteId(workspace: GatewayWorkspace): string | null {
  return workspace.remote?.remote_workspace_id ?? null;
}

export default function GatewayRoutingOverview({
  groups,
  activeWorkspace,
  activeWorkspaceId,
  checkedAt,
  gatewayAvailable,
  switching,
  busyWorkspaceId,
  removingWorkspaceIds,
  reconnectingWorkspaceId,
  restartMode,
  onActivate,
  onUseWorkspace,
  onRename,
  onMove,
  onRuntimeAction,
  onSafeRestart,
  onForceRestart,
  onReconnectGateway,
  onRemove,
}: GatewayRoutingOverviewProps) {
  return (
    <div className="gateway-routing-layout">
      <section
        className="gateway-surface gateway-route-targets"
        aria-labelledby="gateway-route-targets-title"
      >
        <header className="gateway-surface-header">
          <div>
            <h2 id="gateway-route-targets-title">路由目标</h2>
            <p>
              未携带工作区 ID 的请求进入默认路由；显式请求可定向到其他目标。
            </p>
          </div>
          <span>
            {groups.reduce((total, group) => total + group.workspaces.length, 0)} 个目标
            {checkedAt ? ` · ${checkedAt.toLocaleTimeString()}` : ""}
          </span>
        </header>

        {groups.length === 0 ? (
          <div className="gateway-console-empty">
            <span
              className="codicon codicon-server-environment"
              aria-hidden="true"
            />
            <strong>尚未注册工作区</strong>
            <p>添加本机工作区或连接远程 Gateway 后才能建立路由。</p>
          </div>
        ) : (
          <div className="gateway-route-groups">
            {groups.map((group) => {
              const siblingIds = group.workspaces.map(
                (workspace) => workspace.workspace_id,
              );
              const remoteWorkspace = group.workspaces.find(
                (workspace) => workspace.connection_kind === "remote_gateway",
              );
              const groupBusy = group.workspaces.some(
                (workspace) =>
                  busyWorkspaceId === workspace.workspace_id ||
                  removingWorkspaceIds.has(workspace.workspace_id),
              );
              return (
                <section className="gateway-route-group" key={group.key}>
                  <header
                    className={`gateway-route-group-header${
                      remoteWorkspace ? " remote" : ""
                    }`}
                  >
                    <span
                      className={`gateway-group-icon codicon ${
                        remoteWorkspace
                          ? "codicon-remote"
                          : "codicon-device-desktop"
                      }`}
                      aria-hidden="true"
                    />
                    <div>
                      <strong>{group.title}</strong>
                      <small title={group.gatewayLabel ?? group.connectionLabel}>
                        {group.connectionLabel}
                        {group.gatewayLabel ? ` · ${group.gatewayLabel}` : ""}
                      </small>
                    </div>
                    {remoteWorkspace ? (
                      <button
                        type="button"
                        className="gateway-compact-button"
                        disabled={groupBusy}
                        onClick={() => void onReconnectGateway(remoteWorkspace)}
                      >
                        <span
                          className={`codicon codicon-debug-restart${
                            reconnectingWorkspaceId === remoteWorkspace.workspace_id
                              ? " codicon-modifier-spin"
                              : ""
                          }`}
                          aria-hidden="true"
                        />
                        {reconnectingWorkspaceId === remoteWorkspace.workspace_id
                          ? "重连中"
                          : "重连"}
                      </button>
                    ) : (
                      <span className="gateway-connection-pill ready">
                        <span />本机控制面
                      </span>
                    )}
                  </header>

                  {group.workspaces.map((workspace, index) => {
                    const active = workspace.workspace_id === activeWorkspaceId;
                    const busy =
                      busyWorkspaceId === workspace.workspace_id ||
                      removingWorkspaceIds.has(workspace.workspace_id);
                    const services = serviceEntries(workspace);
                    const configReload = workspace.config_reload;
                    return (
                      <article
                        key={workspace.workspace_id}
                        className={`gateway-route-workspace${
                          active ? " active" : ""
                        }${workspace.status === "offline" ? " offline" : ""}`}
                      >
                        <span
                          className={`gateway-workspace-status ${workspace.status}`}
                          aria-hidden="true"
                        />
                        <div className="gateway-route-workspace-copy">
                          <div className="gateway-route-workspace-title">
                            <h3>{workspace.name}</h3>
                            {active ? <span className="gateway-active-badge">默认路由</span> : null}
                            {workspace.connection_kind === "remote_gateway" ? (
                              <span className="gateway-meta-badge">远程投影</span>
                            ) : workspace.managed ? (
                              <span className="gateway-meta-badge">本机托管</span>
                            ) : (
                              <span className="gateway-meta-badge">外部后端</span>
                            )}
                            {workspace.system_default ? (
                              <span className="gateway-meta-badge">系统默认</span>
                            ) : null}
                          </div>
                          <span
                            className="gateway-route-workspace-path"
                            title={workspace.root_path}
                          >
                            {workspace.root_path}
                          </span>
                          <span
                            className="gateway-route-workspace-id"
                            title={workspace.workspace_id}
                          >
                            {workspace.workspace_id}
                            {workspaceRemoteId(workspace)
                              ? ` · remote ${workspaceRemoteId(workspace)}`
                              : ""}
                          </span>

                          {services.length > 0 ? (
                            <div className="gateway-route-services">
                              {services.map(([name, service]) => (
                                <details
                                  key={name}
                                  className={`gateway-route-service ${service.status}`}
                                >
                                  <summary>
                                    <span />
                                    {serviceLabel(name)}
                                    <code>{serviceRouteLabel(service)}</code>
                                  </summary>
                                  <div>
                                    <span>健康检查：{service.health_path}</span>
                                    <span>Gateway 端点：{service.local_url ?? "未建立"}</span>
                                    {service.error ? <strong>{service.error}</strong> : null}
                                  </div>
                                </details>
                              ))}
                            </div>
                          ) : null}

                          {workspace.connection_error ? (
                            <div className="gateway-inline-error" role="status">
                              <span className="codicon codicon-warning" aria-hidden="true" />
                              {workspace.connection_error}
                            </div>
                          ) : null}
                          {configReload?.available && configReload.restart_required ? (
                            <div className="gateway-inline-warning" role="status">
                              <span className="codicon codicon-warning" aria-hidden="true" />
                              配置变更需要重启后端：
                              {configReload.changed_sections.join("、") || "未标明"}
                            </div>
                          ) : null}
                        </div>

                        <div className="gateway-route-workspace-actions">
                          {!active ? (
                            <button
                              type="button"
                              disabled={busy || switching || workspace.status !== "ready"}
                              onClick={() => void onActivate(workspace)}
                            >
                              设为默认
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="primary"
                            disabled={busy || workspace.status !== "ready"}
                            onClick={() => void onUseWorkspace(workspace)}
                          >
                            打开工作台
                          </button>
                          <details className="gateway-workspace-menu">
                            <summary aria-label={`${workspace.name} 更多操作`}>
                              <span className="codicon codicon-ellipsis" aria-hidden="true" />
                            </summary>
                            <div>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => onRename(workspace)}
                              >
                                <span className="codicon codicon-edit" aria-hidden="true" />
                                重命名
                              </button>
                              <button
                                type="button"
                                disabled={index === 0 || busy}
                                onClick={() => void onMove(
                                  workspace.workspace_id,
                                  siblingIds,
                                  -1,
                                )}
                              >
                                <span className="codicon codicon-arrow-up" aria-hidden="true" />
                                上移
                              </button>
                              <button
                                type="button"
                                disabled={index === group.workspaces.length - 1 || busy}
                                onClick={() => void onMove(
                                  workspace.workspace_id,
                                  siblingIds,
                                  1,
                                )}
                              >
                                <span className="codicon codicon-arrow-down" aria-hidden="true" />
                                下移
                              </button>
                              {workspace.runtime_action === "safe_restart_managed_backend" ? (
                                <>
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => void onSafeRestart(workspace)}
                                  >
                                    <span className="codicon codicon-debug-restart" aria-hidden="true" />
                                    {busy && restartMode === "safe"
                                      ? "安全排空中"
                                      : "安全重启后端"}
                                  </button>
                                  <button
                                    type="button"
                                    className="danger"
                                    disabled={busy}
                                    onClick={() => void onForceRestart(workspace)}
                                  >
                                    <span className="codicon codicon-warning" aria-hidden="true" />
                                    {busy && restartMode === "force"
                                      ? "强制重启中"
                                      : "强制重启后端"}
                                  </button>
                                </>
                              ) : workspace.runtime_action ? (
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => void onRuntimeAction(workspace)}
                                >
                                  <span className="codicon codicon-pulse" aria-hidden="true" />
                                  {workspace.runtime_action === "reconnect_remote_gateway"
                                    ? "重连远程 Gateway"
                                    : "重新探测后端"}
                                </button>
                              ) : null}
                              {workspace.removable ? (
                                <button
                                  type="button"
                                  className="danger"
                                  disabled={busy}
                                  onClick={() => onRemove(
                                    workspace.workspace_id,
                                    workspace.name,
                                  )}
                                >
                                  <span className="codicon codicon-trash" aria-hidden="true" />
                                  {removingWorkspaceIds.has(workspace.workspace_id)
                                    ? "移除中"
                                    : "移除"}
                                </button>
                              ) : null}
                            </div>
                          </details>
                        </div>
                      </article>
                    );
                  })}
                </section>
              );
            })}
          </div>
        )}
      </section>

      <aside
        className="gateway-surface gateway-current-route"
        aria-labelledby="gateway-current-route-title"
      >
        <header className="gateway-surface-header">
          <div>
            <h2 id="gateway-current-route-title">当前路由</h2>
            <p>浏览器同源请求的实际转发链路。</p>
          </div>
          <span className={`gateway-connection-pill ${gatewayAvailable ? "ready" : "offline"}`}>
            <span />{gatewayAvailable ? "正常" : "异常"}
          </span>
        </header>
        <div className="gateway-current-route-body">
          <div className="gateway-route-flow" aria-label="Gateway 请求链路">
            <div>
              <span className="codicon codicon-browser" aria-hidden="true" />
              <strong>Web UI</strong><small>同源 /api</small>
            </div>
            <span className="codicon codicon-arrow-right" aria-hidden="true" />
            <div>
              <span className="codicon codicon-server-process" aria-hidden="true" />
              <strong>Gateway</strong><small>控制面</small>
            </div>
            <span className="codicon codicon-arrow-right" aria-hidden="true" />
            <div>
              <span className="codicon codicon-root-folder" aria-hidden="true" />
              <strong>{activeWorkspace?.name ?? "未选择"}</strong>
              <small>Workspace API</small>
            </div>
          </div>
          <dl className="gateway-route-details">
            <div><dt>活动工作区</dt><dd>{activeWorkspace?.workspace_id ?? "无"}</dd></div>
            <div><dt>连接类型</dt><dd>{activeWorkspace ? workspaceKindLabel(activeWorkspace) : "无"}</dd></div>
            <div><dt>目标后端</dt><dd>{activeWorkspace?.backend_url ?? "无"}</dd></div>
            <div><dt>探测时间</dt><dd>{checkedAt?.toLocaleString() ?? "尚未探测"}</dd></div>
          </dl>
          <div className="gateway-routing-note">
            <span className="codicon codicon-info" aria-hidden="true" />
            路由详情只解释控制面选择，不复制工作区业务状态。
          </div>
        </div>
      </aside>
    </div>
  );
}
