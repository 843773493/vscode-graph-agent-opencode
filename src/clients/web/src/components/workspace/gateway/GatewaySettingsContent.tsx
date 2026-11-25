import { lazy, Suspense, useEffect, useState } from 'react';
import { t } from '../../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, type GatewayRequestTarget } from '../../../api/workspaceIdentity';
import { getGatewayHealth, type GatewayLogTarget } from '../../../gatewayApi';
import { errorSummary } from '../../../utils/errorMessage';
import type { GatewayHealth, GatewayWorkspace, AddRemoteGatewayConnectionRequest } from '../../../types/backend';
import type { AppearanceBackground } from '../../../theme/theme';
import type { GatewayBackendRuntimeActions } from './GatewayBackendRuntimePanel';
const Connections = lazy(() => import('./GatewayConnectionsPanel'));
const Devices = lazy(() => import('./GatewayInboundAccessPanel'));
const Runtime = lazy(() => import('./GatewayBackendRuntimePanel'));
const Diagnostics = lazy(() => import('./GatewayDiagnosticsPanel'));
const Theme = lazy(() => import('./GatewayBackgroundAssets'));
const ConnectionDialog = lazy(() => import('./GatewayConnectionDialog'));

export type GatewaySettingsPage = 'connections' | 'credentials' | 'runtime' | 'diagnostics' | 'theme';
export interface GatewaySettingsContentProps extends GatewayBackendRuntimeActions {
  page: GatewaySettingsPage;
  entryGatewayId: string;
  workspaces: GatewayWorkspace[];
  revision: number;
  target: GatewayRequestTarget;
  dialog?: 'ssh' | 'external-device';
  onCloseDialog: () => void;
  onChanged: () => void;
  onAddRemoteGatewayConnection: (payload: AddRemoteGatewayConnectionRequest) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRemoteGatewayReconnected: (id: string) => void;
  onOpenConfiguration: (target?: GatewayRequestTarget) => Promise<void>;
  onOpenLogs: (gatewayConnectionId: string) => void;
  onOpenLog: (target: GatewayLogTarget & { log_id: string }) => Promise<void>;
  background: AppearanceBackground;
  onUpdateBackground: (value: AppearanceBackground) => Promise<void>;
}

/** 只渲染原设置 Pane 的管理内容；分类、范围与工具栏均由原生 Workbench 持有。 */
export default function GatewaySettingsContent(props: GatewaySettingsContentProps) {
  const [health, setHealth] = useState<{ data?: GatewayHealth; error?: unknown }>();
  useEffect(() => {
    if (props.page !== 'connections') return;
    let active = true;
    void getGatewayHealth(0).then(data => {
      if (data.gateway_id !== props.entryGatewayId) throw new Error(t('gateway.connections.ownerMismatch'));
      if (active) setHealth({ data });
    }).catch(cause => { if (active) setHealth({ error: cause }); });
    return () => { active = false; };
  }, [props.page, props.entryGatewayId, props.revision]);
  return <div className="gateway-settings-content" data-management-page={props.page}>
    <Suspense fallback={<p role="status">{t('gateway.control.loadingPage')}</p>}>
      {props.page === 'connections' ? <>
        <div className="gateway-settings-local" aria-label={t('gateway.control.localGatewayConnection')}>
          <span className="codicon codicon-device-desktop" aria-hidden="true" />
          <strong>{t('workbench.managementTarget.local')}</strong>
          <span>{health?.error ? t('gateway.control.errorStatus') : health?.data ? t('gateway.control.available') : t('gateway.control.loadingPage')}</span>
          <small>{props.entryGatewayId}</small>
        </div>
        {health?.error ? <p className="failure-summary" role="alert">{errorSummary(health.error)}</p> : null}
        <Connections apiPort={0} gatewayId={props.entryGatewayId} workspaces={props.workspaces} revision={props.revision}
          onAddRemoteGatewayConnection={props.onAddRemoteGatewayConnection} onRefresh={props.onRefresh} onRemoteGatewayReconnected={props.onRemoteGatewayReconnected}
          onOpenConfiguration={props.onOpenConfiguration} onOpenLogs={props.onOpenLogs} />
      </> : props.page === 'credentials' ? <Devices apiPort={0} revision={props.revision} />
        : props.page === 'runtime' ? <Runtime workspaces={props.workspaces} workspaceKey={props.target.workspace_id ? gatewayWorkspaceKey({ gatewayId: props.target.gateway_id, workspaceId: props.target.workspace_id }) : undefined} onStart={props.onStart} onStop={props.onStop}
          onSafeRestart={props.onSafeRestart} onForceRestart={props.onForceRestart} />
        : props.page === 'theme' ? <Theme key={props.revision} apiPort={0} background={props.background} onUpdateBackground={props.onUpdateBackground} />
        : <Diagnostics key={props.revision} apiPort={0} target={props.target} onOpenRetention={props.onOpenConfiguration} onOpenLog={props.onOpenLog} />}
      {props.dialog ? <ConnectionDialog open mode={props.dialog} apiPort={0} onClose={props.onCloseDialog}
        onAddRemoteGatewayConnection={async payload => { await props.onAddRemoteGatewayConnection(payload); props.onChanged(); }}
        onDeviceConnectionsChanged={props.onChanged} /> : null}
    </Suspense>
  </div>;
}
