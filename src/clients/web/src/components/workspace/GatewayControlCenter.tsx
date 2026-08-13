import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getGatewayHealth,
  restartDevelopmentRuntime,
} from "../../gatewayApi";
import type {
  AddSshGatewayWorkspaceRequest,
  GatewayHealth,
  GatewayWorkspace,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import { useWarmConfirm } from "../WarmConfirmProvider";
import GatewayConnectionDialog from "./GatewayConnectionDialog";
import GatewayInboundAccessPanel from "./GatewayInboundAccessPanel";
import { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";
import GatewayDiagnosticsPanel from "./GatewayDiagnosticsPanel";
import GatewayThemeSettings from "./GatewayThemeSettings";

export { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";

interface GatewayControlCenterProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  gatewayError: string | null;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
  onRefresh: () => Promise<void>;
  onReconnect: (workspaceId: string) => Promise<void>;
  uiSettings: WebUiSettings;
  onUpdateUiSettings: (update: WebUiSettingsUpdate) => Promise<void>;
}

export default function GatewayControlCenter({
  apiPort,
  workspaces,
  gatewayError,
  onAddSsh,
  onRefresh,
  onReconnect,
  uiSettings,
  onUpdateUiSettings,
}: GatewayControlCenterProps) {
  const confirm = useWarmConfirm();
  const [health, setHealth] = useState<GatewayHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [connectionDialogMode, setConnectionDialogMode] = useState<
    "ssh" | "external-device" | null
  >(null);
  const [reconnectingId, setReconnectingId] = useState<string | null>(null);
  const [restartingDevelopment, setRestartingDevelopment] = useState(false);
  const [deviceRevision, setDeviceRevision] = useState(0);
  const [activePage, setActivePage] = useState<"connections" | "theme" | "diagnostics">("connections");

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await getGatewayHealth(apiPort));
      setHealthError(null);
    } catch (error) {
      setHealth(null);
      setHealthError(error instanceof Error ? error.message : String(error));
    }
  }, [apiPort]);

  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  const remoteGatewayGroups = useMemo(
    () => groupGatewayWorkspaces(workspaces).filter((group) => group.key.startsWith("remote:")),
    [workspaces],
  );

  const handleRefresh = async () => {
    setRefreshing(true);
    setOperationError(null);
    setOperationNotice(null);
    try {
      await Promise.all([onRefresh(), loadHealth()]);
      setDeviceRevision((value) => value + 1);
      setOperationNotice("连接状态已刷新。");
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    } finally {
      setRefreshing(false);
    }
  };

  const waitForRestartedGateway = async (previousProcessId: number) => {
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      try {
        const nextHealth = await getGatewayHealth(apiPort);
        if (nextHealth.process_id !== previousProcessId) return;
      } catch {
        // 服务重启期间连接失败是预期状态；继续等待新 Gateway 就绪。
      }
    }
    throw new Error("本机开发服务在 120 秒内未恢复，请检查 development-restart.log");
  };

  const handleDevelopmentRestart = async () => {
    const accepted = await confirm({
      title: "重启本机开发服务",
      message:
        "将重启 Web、Gateway、Terminal 前端和工作区运行时。当前页面会短暂断开，正在运行的 Agent 与终端可能被中断。",
      confirmText: "确认重启",
    });
    if (!accepted) return;
    setRestartingDevelopment(true);
    setOperationError(null);
    setOperationNotice("正在重启本机开发服务，恢复后将自动刷新页面…");
    try {
      const result = await restartDevelopmentRuntime(apiPort);
      await waitForRestartedGateway(result.previous_process_id);
      window.location.reload();
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
      setOperationNotice(null);
      setRestartingDevelopment(false);
    }
  };

  const reconnect = async (workspace: GatewayWorkspace) => {
    if (!workspace.remote) {
      setOperationError(`远程连接 ${workspace.workspace_id} 缺少 Gateway 摘要`);
      return;
    }
    const accepted = await confirm({
      title: "重新连接远程 Gateway",
      message: `重新连接“${workspace.remote.ssh_config_host ?? workspace.remote.host}”？\n\n该 Gateway 下的请求会短暂中断。`,
      confirmText: "重新连接",
    });
    if (!accepted) return;
    setReconnectingId(workspace.remote.gateway_connection_id);
    setOperationError(null);
    setOperationNotice(null);
    try {
      await onReconnect(workspace.workspace_id);
      await loadHealth();
      setOperationNotice(`远程 Gateway「${workspace.remote.ssh_config_host ?? workspace.remote.host}」已重新连接。`);
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
    } finally {
      setReconnectingId(null);
    }
  };

  const available = health?.status === "ok";

  return (
    <main className="gateway-control-shell">
      <aside className="gateway-control-sidebar" data-bt-surface="chrome">
        <div className="gateway-control-brand">
          <span className="codicon codicon-server-process" aria-hidden="true" />
          <div><strong>BoxTeam</strong><small>Local Control UI</small></div>
        </div>
        <nav aria-label="Gateway 控制面导航">
          <section>
            <p>控制面</p>
            <button type="button" className={activePage === "connections" ? "active" : undefined} aria-current={activePage === "connections" ? "page" : undefined} onClick={() => setActivePage("connections")}>
              <span className="codicon codicon-plug" aria-hidden="true" />
              连接管理
              <small>{remoteGatewayGroups.length}</small>
            </button>
          </section>
          <section>
            <p>系统</p>
            <button type="button" className={activePage === "theme" ? "active" : undefined} aria-current={activePage === "theme" ? "page" : undefined} onClick={() => setActivePage("theme")}><span className="codicon codicon-color-mode" aria-hidden="true" />主题设置</button>
            <button type="button" disabled><span className="codicon codicon-pulse" aria-hidden="true" />服务运行时</button>
            <button type="button" disabled><span className="codicon codicon-shield" aria-hidden="true" />连接与凭据</button>
            <button type="button" className={activePage === "diagnostics" ? "active" : undefined} aria-current={activePage === "diagnostics" ? "page" : undefined} onClick={() => setActivePage("diagnostics")}><span className="codicon codicon-output" aria-hidden="true" />日志与诊断</button>
          </section>
        </nav>
        <div className="gateway-control-sidebar-status">
          <strong><span className={available ? undefined : "offline"} />{available ? "Gateway 正常" : "Gateway 异常"}</strong>
          <code>127.0.0.1:{apiPort}</code>
          <small>{remoteGatewayGroups.length} 个远程 Gateway 连接</small>
        </div>
      </aside>

      <section className="gateway-control-main" aria-labelledby="gateway-control-title">
        <div className="gateway-control-content gateway-connections-content">
          <header className="gateway-control-header">
            <div>
              <span>本地控制面</span>
              <h1 id="gateway-control-title">{activePage === "connections" ? "连接管理" : activePage === "theme" ? "主题设置" : "日志与诊断"}</h1>
              <p>{activePage === "connections" ? "管理远程 Gateway、手机和其他外部设备的连接与授权。工作区请在会话工作台中管理。" : activePage === "theme" ? "统一切换界面配色并管理网络或本地背景图片。" : "从连接的 Gateway 逐层查看控制面和托管工作区的运行日志，定位连接、启动与代理问题。"}</p>
            </div>
            {activePage === "connections" ? <div className="gateway-control-actions">
              {health?.development_restart_available ? (
                <button
                  type="button"
                  className="danger-soft"
                  disabled={restartingDevelopment}
                  onClick={() => void handleDevelopmentRestart()}
                >
                  <span
                    className={`codicon codicon-debug-restart${restartingDevelopment ? " codicon-modifier-spin" : ""}`}
                    aria-hidden="true"
                  />
                  {restartingDevelopment ? "正在重启…" : "重启本机服务"}
                </button>
              ) : null}
              <button type="button" className="primary" onClick={() => setConnectionDialogMode("ssh")}>
                <span className="codicon codicon-add" aria-hidden="true" />添加 SSH 连接
              </button>
              <button type="button" className="icon-only" aria-label="刷新连接" title="刷新连接" disabled={refreshing} onClick={() => void handleRefresh()}>
                <span className={`codicon codicon-refresh${refreshing ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
              </button>
            </div> : null}
          </header>

          {activePage === "theme" ? (
            <GatewayThemeSettings apiPort={apiPort} settings={uiSettings} onUpdateSettings={onUpdateUiSettings} />
          ) : activePage === "diagnostics" ? (
            <GatewayDiagnosticsPanel apiPort={apiPort} workspaces={workspaces} />
          ) : <>

          {gatewayError || healthError || operationError ? (
            <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" aria-hidden="true" /><div><strong>连接操作失败</strong><span>{gatewayError ?? healthError ?? operationError}</span></div></div>
          ) : null}
          {operationNotice ? <div className="gateway-console-notice" role="status"><span className="codicon codicon-pass-filled" aria-hidden="true" />{operationNotice}</div> : null}

          <section className="gateway-local-connection-card" aria-label="本机 Gateway 连接">
            <span className="gateway-device-icon"><span className="codicon codicon-device-desktop" aria-hidden="true" /></span>
            <div><div><strong>本机 Gateway</strong><span className={`gateway-authorization-status ${available ? "authorized" : "expired"}`}>{available ? "可用" : "异常"}</span></div><small>127.0.0.1:{apiPort} · 当前控制面</small></div>
          </section>

          <section className="gateway-connection-section" aria-labelledby="remote-gateway-connections-title">
            <div className="gateway-connection-section-heading">
              <div><h2 id="remote-gateway-connections-title">远程 Gateway</h2><p>通过 SSH 隧道连接其他电脑上的 Gateway。</p></div>
              <span>{remoteGatewayGroups.length} 个连接</span>
            </div>
            {remoteGatewayGroups.length > 0 ? (
              <div className="gateway-remote-connection-list">
                {remoteGatewayGroups.map((group) => {
                  const workspace = group.workspaces[0];
                  const remote = workspace?.remote;
                  if (!workspace || !remote) throw new Error(`远程 Gateway 分组 ${group.key} 缺少连接摘要`);
                  const connectionError = group.workspaces.find(
                    (item) => item.connection_error,
                  )?.connection_error;
                  const ready = !connectionError;
                  const isReconnecting = reconnectingId === remote.gateway_connection_id;
                  return (
                    <article key={group.key}>
                      <span className="gateway-device-icon"><span className="codicon codicon-remote" aria-hidden="true" /></span>
                      <div className="gateway-device-copy">
                        <div><strong>{group.title}</strong><span className={`gateway-authorization-status ${ready ? "authorized" : "expired"}`}>{ready ? "已连接" : "连接异常"}</span></div>
                        <small>{connectionError ?? group.connectionLabel}</small>
                        <code title={remote.gateway_id}>{remote.gateway_id}</code>
                      </div>
                      <div className="gateway-remote-connection-meta"><span>{group.workspaces.length} 个工作区</span><button type="button" disabled={isReconnecting} onClick={() => void reconnect(workspace)}>{isReconnecting ? "连接中…" : "重新连接"}</button></div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="gateway-connection-empty"><span className="codicon codicon-remote" aria-hidden="true" /><strong>尚未连接远程 Gateway</strong><p>点击“添加 SSH 连接”，从 ~/.ssh/config 选择远程主机或手动添加。</p></div>
            )}
          </section>

          <GatewayInboundAccessPanel
            apiPort={apiPort}
            revision={deviceRevision}
            onAddDevice={() => setConnectionDialogMode("external-device")}
          />
          </>}
        </div>
      </section>

      <GatewayConnectionDialog
        open={connectionDialogMode !== null}
        mode={connectionDialogMode ?? "ssh"}
        apiPort={apiPort}
        onClose={() => setConnectionDialogMode(null)}
        onAddSsh={onAddSsh}
        onDeviceConnectionsChanged={() => setDeviceRevision((value) => value + 1)}
      />
    </main>
  );
}
