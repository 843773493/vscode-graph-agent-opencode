import { useContext, useState } from "react";
import { createPortal } from "react-dom";
import { t } from "../../../../shared/core/i18n/index.js";
import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { HttpRequestError } from "../../api/http";
import type { GatewayWorkspace } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";
import { OverlayRootContext } from "../overlays/OverlayRootContext";

export interface WorkspaceDetailsTarget { workspaceKey: string; entryGatewayId: string }

interface WorkspaceDetailsDialogProps {
  target: WorkspaceDetailsTarget;
  workspaces: GatewayWorkspace[];
  onClose: () => void;
  onOpen: (workspaceKey: string) => Promise<void>;
  onRename: (workspace: GatewayWorkspace) => void;
  onRemove: (workspaceKey: string, name: string) => void;
  onOpenSettings: (workspace: GatewayWorkspace, connectionId?: string) => Promise<void>;
  onOpenLogs: (workspace: GatewayWorkspace, connectionId?: string) => Promise<void>;
}

export default function WorkspaceDetailsDialog({ target, workspaces, onClose, onOpen,
  onRename, onRemove, onOpenSettings, onOpenLogs,
}: WorkspaceDetailsDialogProps) {
  const overlayRoot = useContext(OverlayRootContext);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const owner = workspaceOwnerFromKey(target.workspaceKey);
  const workspace = workspaces.find(item => gatewayWorkspaceKey(item) === target.workspaceKey);
  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(undefined);
    try { await operation(); onClose(); }
    catch (cause) {
      const message = cause instanceof HttpRequestError && cause.requestId
        ? `${errorMessage(cause)} · request_id=${cause.requestId}` : errorMessage(cause);
      setError(t("gateway.workspaceDetails.actionFailed", owner.gatewayId, owner.workspaceId, message));
    } finally { setBusy(false); }
  };
  return createPortal(<div className="workspace-dialog-backdrop" role="presentation">
    <section className="workspace-dialog workspace-details-dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-details-title"
      onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
      <header className="workspace-dialog-header workspace-dialog-heading">
        <div><h2 id="workspace-details-title">{t("gateway.workspaceDetails.title")}</h2><p>{workspace?.name ?? owner.workspaceId}</p></div>
        <button autoFocus type="button" className="workspace-dialog-icon-button" aria-label={t("gateway.workspaceDetails.close")} onClick={onClose}>×</button>
      </header>
      <dl className="workspace-dialog-grid">
        <div><dt>{t("gateway.workspaceDetails.entryGateway")}</dt><dd><code>{target.entryGatewayId}</code></dd></div>
        <div><dt>{t("gateway.workspaceDetails.targetGateway")}</dt><dd><code>{owner.gatewayId}</code></dd></div>
        <div className="workspace-dialog-wide"><dt>Workspace ID</dt><dd><code>{owner.workspaceId}</code></dd></div>
        {workspace ? <>
          <div><dt>{t("gateway.workspaceDetails.status")}</dt><dd>{workspace.status} · {workspace.runtime_action}</dd></div>
          <div><dt>{t("gateway.workspaceDetails.backend")}</dt><dd><code>{workspace.backend_id ?? t("gateway.workspaceDetails.unassigned")}</code></dd></div>
          <div className="workspace-dialog-wide"><dt>{t("gateway.workspaceDetails.generation")}</dt><dd><code>{workspace.backend_generation ?? t("gateway.workspaceDetails.unassigned")}</code></dd></div>
          <div className="workspace-dialog-wide"><dt>{t("gateway.workspaceDetails.endpoint")}</dt><dd><code>{workspace.backend_url}</code></dd></div>
          <div className="workspace-dialog-wide"><dt>{t("gateway.workspaceDetails.members")}</dt><dd><ul>{workspace.affected_workspace_ids.map(id => {
            const member = workspaces.find(item => item.gateway_id === owner.gatewayId && item.workspace_id === id);
            return <li key={id}>{member?.name} <code>{id}</code></li>;
          })}</ul></dd></div>
          <div><dt>{t("gateway.workspaceDetails.registration")}</dt><dd>{t(workspace.managed ? "gateway.workspaceDetails.managed" : "gateway.workspaceDetails.external")}{workspace.system_default ? ` · ${t("gateway.workspaceDetails.systemDefault")}` : ""}</dd></div>
          <div><dt>{t("gateway.workspaceDetails.checkedAt")}</dt><dd>{workspace.checked_at}</dd></div>
          <div className="workspace-dialog-wide"><dt>{t("gateway.workspaceDetails.connections")}</dt><dd>{workspace.connection_kind}{workspace.remote_gateways.map(connection => <p key={connection.gateway_connection_id}>
            {connection.name} <code>{connection.gateway_connection_id}</code> · {connection.username}@{connection.host}:{connection.port}
            <button type="button" disabled={busy || workspace.status !== "ready"} onClick={() => void run(() => onOpenSettings(workspace, connection.gateway_connection_id))}>{t("gateway.workspaceDetails.settings")}</button>
            <button type="button" disabled={busy} onClick={() => void run(() => onOpenLogs(workspace, connection.gateway_connection_id))}>{t("gateway.workspaceDetails.logs")}</button>
          </p>)}</dd></div>
        </> : null}
      </dl>
      {!workspace ? <p className="workspace-dialog-error" role="alert">{t("gateway.workspaceDetails.unavailable")}</p> : null}
      {workspace?.connection_error ? <p className="workspace-dialog-error" role="alert">{workspace.connection_error}</p> : null}
      {error ? <p className="workspace-dialog-error" role="alert">{error}</p> : null}
      <p className="workspace-action-dialog-description">{t("gateway.workspaceDetails.rootBinding")}</p>
      <p className="workspace-action-dialog-description">{t("gateway.workspaceDetails.removeBoundary")}</p>
      <footer className="workspace-dialog-actions">
        {workspace ? <>
          <button type="button" disabled={busy || workspace.status !== "ready"} onClick={() => void run(() => onOpen(target.workspaceKey))}>{t("gateway.workspaceDetails.open")}</button>
          <button type="button" disabled={busy} onClick={() => { onRename(workspace); onClose(); }}>{t("navigation.rename")}</button>
          {workspace.connection_kind !== "remote_gateway" ? <>
            <button type="button" disabled={busy || workspace.status !== "ready"} onClick={() => void run(() => onOpenSettings(workspace))}>{t("gateway.workspaceDetails.settings")}</button>
            <button type="button" disabled={busy} onClick={() => void run(() => onOpenLogs(workspace))}>{t("gateway.workspaceDetails.logs")}</button>
          </> : null}
          {workspace.removable ? <button type="button" className="danger" disabled={busy} onClick={() => { onClose(); onRemove(target.workspaceKey, workspace.name); }}>{t("gateway.workspaceDetails.remove")}</button> : null}
        </> : null}
        <button type="button" onClick={onClose}>{t("gateway.workspaceDetails.close")}</button>
      </footer>
    </section>
  </div>, overlayRoot ?? document.body);
}
