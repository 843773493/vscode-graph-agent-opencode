import type { AppearanceBackground } from "../../../theme/theme";
import { t } from "../../../../../shared/core/i18n/index.js";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import {
  getGatewayHealth,
  reconnectRemoteGatewayConnection,
  restartDevelopmentRuntime,
} from "../../../gatewayApi";
import type {
  AddSshGatewayWorkspaceRequest,
  GatewayHealth,
  GatewayWorkspace,
  GatewayRemoteConnectionSummary,
} from "../../../types/backend";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import type { GatewayBackendRuntimeActions } from "./GatewayBackendRuntimePanel";
import { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";
import { errorMessage } from "../../../utils/errorMessage";
import type { GatewayDiagnosticLogLocateRequest } from "../../../types/protocol_generated/boxteam/gateway/v2/public";
import GatewayDiagnosticLogLocation from "./GatewayDiagnosticLogLocation";

const GatewayBackendRuntimePanel = lazy(() => import("./GatewayBackendRuntimePanel"));
const GatewayConnectionDialog = lazy(() => import("./GatewayConnectionDialog"));
const GatewayDiagnosticsPanel = lazy(() => import("./GatewayDiagnosticsPanel"));
const GatewayThemeSettings = lazy(() => import("./GatewayThemeSettings"));
const GatewayInboundAccessPanel = lazy(() => import("./GatewayInboundAccessPanel"));

export type GatewayControlPage = "connections" | "credentials" | "theme" | "diagnostics" | "runtime";
export interface GatewayControlRequest { page: GatewayControlPage }

export { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";

interface GatewayControlCenterProps extends GatewayBackendRuntimeActions {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  gatewayError: string | null;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRemoteGatewayReconnected: (connectionId: string) => void;
  background: AppearanceBackground;
  onUpdateBackground: (value: AppearanceBackground) => Promise<void>;
  onSelectColorTheme: () => Promise<void>;
  request?: GatewayControlRequest;
  diagnosticLocation?: GatewayDiagnosticLogLocateRequest;
  onCloseDiagnosticLocation?: () => void;
}

export default function GatewayControlCenter({
  apiPort,
  workspaces,
  gatewayError,
  onAddSsh,
  onRefresh,
  onRemoteGatewayReconnected,
  onStart, onStop, onSafeRestart, onForceRestart,
  background,
  onUpdateBackground,
  onSelectColorTheme,
  request,
  diagnosticLocation, onCloseDiagnosticLocation,
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
  const [activePage, setActivePage] = useState<GatewayControlPage>(request?.page ?? "connections");
  useEffect(() => { if (request) setActivePage(request.page); }, [request]);
  useEffect(() => { if (diagnosticLocation) setActivePage("diagnostics"); }, [diagnosticLocation]);

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await getGatewayHealth(apiPort));
      setHealthError(null);
    } catch (error) {
      setHealth(null);
      setHealthError(errorMessage(error));
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
      setOperationNotice(t("gateway.control.refreshed"));
    } catch (error) {
      setOperationError(errorMessage(error));
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
    throw new Error(t("gateway.control.restartTimedOut", 120));
  };

  const handleDevelopmentRestart = async () => {
    const accepted = await confirm({
      title: t("gateway.control.restartTitle"),
      message: t("gateway.control.restartDetail"),
      confirmText: t("gateway.control.restartConfirm"),
    });
    if (!accepted) return;
    setRestartingDevelopment(true);
    setOperationError(null);
    setOperationNotice(t("gateway.control.restarting"));
    try {
      const result = await restartDevelopmentRuntime(apiPort);
      await waitForRestartedGateway(result.previous_process_id);
      window.location.reload();
    } catch (error) {
      setOperationError(errorMessage(error));
      setOperationNotice(null);
      setRestartingDevelopment(false);
    }
  };

  const reconnect = async (remote: GatewayRemoteConnectionSummary) => {
    const accepted = await confirm({
      title: t("gateway.control.reconnectTitle"),
      message: t("gateway.control.reconnectDetail", remote.ssh_config_host ?? remote.host),
      confirmText: t("gateway.control.reconnect"),
    });
    if (!accepted) return;
    setReconnectingId(remote.gateway_connection_id);
    setOperationError(null);
    setOperationNotice(null);
    try {
      await reconnectRemoteGatewayConnection(apiPort, remote.gateway_connection_id);
      onRemoteGatewayReconnected(remote.gateway_connection_id);
      await onRefresh();
      await loadHealth();
      setOperationNotice(t("gateway.control.reconnected", remote.ssh_config_host ?? remote.host));
    } catch (error) {
      setOperationError(errorMessage(error));
    } finally {
      setReconnectingId(null);
    }
  };

  const available = health?.status === "ok";
  const feedbackError = gatewayError || healthError || operationError;

  return (
    <main className="gateway-control-shell">
      <aside className="gateway-control-sidebar" data-bt-surface="chrome">
        <div className="gateway-control-brand">
          <span className="codicon codicon-server-process" aria-hidden="true" />
          <div><strong>BoxTeam</strong><small>{t("gateway.control.brandSubtitle")}</small></div>
        </div>
        <nav aria-label={t("gateway.control.navigation")}>
          <section>
            <p>{t("gateway.control.section")}</p>
            <button type="button" className={activePage === "connections" ? "active" : undefined} aria-current={activePage === "connections" ? "page" : undefined} onClick={() => setActivePage("connections")}>
              <span className="codicon codicon-plug" aria-hidden="true" />
              {t("gateway.control.connections")}
              <small>{remoteGatewayGroups.length}</small>
            </button>
          </section>
          <section>
            <p>{t("gateway.control.system")}</p>
            <button type="button" className={activePage === "theme" ? "active" : undefined} aria-current={activePage === "theme" ? "page" : undefined} onClick={() => setActivePage("theme")}><span className="codicon codicon-color-mode" aria-hidden="true" />{t("gateway.control.theme")}</button>
            <button type="button" className={activePage === "runtime" ? "active" : undefined} aria-current={activePage === "runtime" ? "page" : undefined} onClick={() => setActivePage("runtime")}><span className="codicon codicon-pulse" aria-hidden="true" />{t("gateway.control.runtime")}</button>
            <button type="button" className={activePage === "credentials" ? "active" : undefined} aria-current={activePage === "credentials" ? "page" : undefined} onClick={() => setActivePage("credentials")}><span className="codicon codicon-shield" aria-hidden="true" />{t("gateway.control.connectionsAndCredentials")}</button>
            <button type="button" className={activePage === "diagnostics" ? "active" : undefined} aria-current={activePage === "diagnostics" ? "page" : undefined} onClick={() => setActivePage("diagnostics")}><span className="codicon codicon-output" aria-hidden="true" />{t("gateway.control.diagnostics")}</button>
          </section>
        </nav>
        <div className="gateway-control-sidebar-status">
          <strong><span className={available ? undefined : "offline"} />{available ? t("gateway.control.gatewayAvailable") : t("gateway.control.gatewayUnavailable")}</strong>
          <code>{health?.gateway_id}</code>
          <small>{t("gateway.control.remoteConnectionCount", remoteGatewayGroups.length)}</small>
        </div>
      </aside>

      <section className="gateway-control-main" aria-labelledby="gateway-control-title">
        <div className="gateway-control-content gateway-connections-content">
          <header className="gateway-control-header">
            <div>
              <span>{t("gateway.control.localControlPlane")}</span>
              <h1 id="gateway-control-title">{activePage === "connections" ? t("gateway.control.connections") : activePage === "credentials" ? t("gateway.control.connectionsAndCredentials") : activePage === "theme" ? t("gateway.control.theme") : activePage === "runtime" ? t("gateway.control.runtime") : t("gateway.control.diagnostics")}</h1>
              <p>{activePage === "connections" ? t("gateway.control.connectionsDescription") : activePage === "credentials" ? t("gateway.control.credentialsDescription") : activePage === "theme" ? t("gateway.control.themeDescription") : activePage === "runtime" ? t("gateway.control.runtimeDescription") : t("gateway.control.diagnosticsDescription")}</p>
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
                  {restartingDevelopment ? t("gateway.control.restartingShort") : t("gateway.control.restartLocal")}
                </button>
              ) : null}
              <button type="button" className="primary" onClick={() => setConnectionDialogMode("ssh")}>
                <span className="codicon codicon-add" aria-hidden="true" />{t("gateway.connection.addSsh")}
              </button>
              <button type="button" className="icon-only" aria-label={t("gateway.control.refreshConnections")} title={t("gateway.control.refreshConnections")} disabled={refreshing} onClick={() => void handleRefresh()}>
                <span className={`codicon codicon-refresh${refreshing ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
              </button>
            </div> : null}
          </header>

          <Suspense fallback={<div role="status" aria-live="polite">{t("gateway.control.loadingPage")}</div>}>
            {activePage === "theme" ? (
              <GatewayThemeSettings apiPort={apiPort} background={background} onUpdateBackground={onUpdateBackground} onSelectColorTheme={onSelectColorTheme} />
            ) : activePage === "runtime" ? (
              <GatewayBackendRuntimePanel workspaces={workspaces} onStart={onStart} onStop={onStop} onSafeRestart={onSafeRestart} onForceRestart={onForceRestart} />
            ) : activePage === "diagnostics" ? (
              diagnosticLocation ? <GatewayDiagnosticLogLocation apiPort={apiPort} request={diagnosticLocation} workspaces={workspaces}
                onClose={() => onCloseDiagnosticLocation?.()} /> : <GatewayDiagnosticsPanel apiPort={apiPort} workspaces={workspaces} />
            ) : activePage === "credentials" ? (
              <GatewayInboundAccessPanel apiPort={apiPort} revision={deviceRevision}
                onAddDevice={() => setConnectionDialogMode("external-device")} />
            ) : <>

          <div className="gateway-console-feedback-slot">
            {feedbackError ? (
              <div
                className="gateway-console-feedback error"
                role="alert"
                title={t("gateway.control.operationFailed", feedbackError)}
              >
                <span className="codicon codicon-error" aria-hidden="true" />
                <strong>{t("gateway.control.operationFailedTitle")}</strong>
                <span className="gateway-console-feedback-message">{feedbackError}</span>
              </div>
            ) : null}
            {operationNotice ? (
              <div
                className="gateway-console-feedback notice"
                role="status"
                title={operationNotice}
              >
                <span className="codicon codicon-pass-filled" aria-hidden="true" />
                <span className="gateway-console-feedback-message">{operationNotice}</span>
              </div>
            ) : null}
          </div>

          <section className="gateway-local-connection-card" aria-label={t("gateway.control.localGatewayConnection")}>
            <span className="gateway-device-icon"><span className="codicon codicon-device-desktop" aria-hidden="true" /></span>
            <div><div><strong>{t("gateway.control.localGateway")}</strong><span className={`gateway-authorization-status ${available ? "authorized" : "expired"}`}>{available ? t("gateway.control.available") : t("gateway.control.errorStatus")}</span></div><small>{t("gateway.control.localEndpoint", `127.0.0.1:${apiPort}`)}</small></div>
          </section>

          <section className="gateway-connection-section" aria-labelledby="remote-gateway-connections-title">
            <div className="gateway-connection-section-heading">
              <div><h2 id="remote-gateway-connections-title">{t("gateway.control.remoteGateways")}</h2><p>{t("gateway.control.remoteDescription")}</p></div>
              <span>{t("gateway.control.connectionCount", remoteGatewayGroups.length)}</span>
            </div>
            {remoteGatewayGroups.length > 0 ? (
              <div className="gateway-remote-connection-list">
                {remoteGatewayGroups.map((group) => {
                  const remote = group.remote;
                  if (!remote) throw new Error(t("gateway.control.remoteGatewaySummaryMissing", group.key));
                  const connectionError = group.workspaces.find(
                    (item) => item.connection_error,
                  )?.connection_error;
                  const ready = !connectionError;
                  const isReconnecting = reconnectingId === remote.gateway_connection_id;
                  return (
                    <article key={group.key}>
                      <span className="gateway-device-icon"><span className="codicon codicon-remote" aria-hidden="true" /></span>
                      <div className="gateway-device-copy">
                        <div><strong>{group.title}</strong><span className={`gateway-authorization-status ${ready ? "authorized" : "expired"}`}>{ready ? t("gateway.control.connected") : t("gateway.control.connectionError")}</span></div>
                        <small>{connectionError ?? group.connectionLabel}</small>
                        <code title={remote.gateway_id}>{remote.gateway_id}</code>
                      </div>
                      <div className="gateway-remote-connection-meta"><span>{t("gateway.control.workspaceCount", group.workspaces.length)}</span><button type="button" disabled={isReconnecting} onClick={() => void reconnect(remote)}>{isReconnecting ? t("gateway.control.connecting") : t("gateway.control.reconnect")}</button></div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="gateway-connection-empty"><span className="codicon codicon-remote" aria-hidden="true" /><strong>{t("gateway.control.noRemoteConnections")}</strong><p>{t("gateway.control.noRemoteConnectionsDetail")}</p></div>
            )}
          </section>

            </>}
          </Suspense>
        </div>
      </section>

      {connectionDialogMode ? (
        <Suspense fallback={(
          <div className="gateway-connection-overlay" role="presentation">
            <div className="gateway-connection-backdrop" aria-hidden="true" />
            <div className="gateway-connection-dialog" role="status" aria-live="polite">
              {t("gateway.connection.loadingDialog")}
            </div>
          </div>
        )}>
          <GatewayConnectionDialog
            open
            mode={connectionDialogMode}
            apiPort={apiPort}
            onClose={() => setConnectionDialogMode(null)}
            onAddSsh={onAddSsh}
            onDeviceConnectionsChanged={() => setDeviceRevision((value) => value + 1)}
          />
        </Suspense>
      ) : null}
    </main>
  );
}
