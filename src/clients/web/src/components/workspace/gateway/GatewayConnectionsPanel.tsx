import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import { HttpRequestError, onGatewayTokenInvalidated } from "../../../api/http";
import {
  listGatewayManagementTargets,
  reconnectRemoteGatewayConnection,
  removeRemoteGatewayConnection,
} from "../../../gatewayApi";
import type { AddSshGatewayWorkspaceRequest, GatewayWorkspace } from "../../../types/backend";
import type { GatewayManagementDirectoryDTO, GatewayManagementTargetDTO } from "../../../types/protocol_generated/boxteam/gateway/v2/public";
import { errorMessage } from "../../../utils/errorMessage";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";

interface GatewayConnectionsPanelProps {
  apiPort: number;
  gatewayId: string | null;
  workspaces: GatewayWorkspace[];
  revision: number;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRemoteGatewayReconnected: (connectionId: string) => void;
  onOpenConfiguration: () => Promise<void>;
  onOpenLogs: (connectionId: string) => void;
}

function connectionFailure(error: unknown): string {
  return error instanceof HttpRequestError && error.requestId
    ? `${errorMessage(error)} · request_id=${error.requestId}` : errorMessage(error);
}

export default function GatewayConnectionsPanel({ apiPort, gatewayId, workspaces, revision,
  onAddSsh, onRefresh, onRemoteGatewayReconnected, onOpenConfiguration, onOpenLogs,
}: GatewayConnectionsPanelProps) {
  const confirm = useWarmConfirm();
  const [directory, setDirectory] = useState<GatewayManagementDirectoryDTO>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>();
  const [editing, setEditing] = useState<{ connectionId: string; name: string }>();
  const scope = useMemo(() => new AbortController(), [apiPort, gatewayId]);
  const readVersion = useRef(0);
  const load = useCallback(async (signal: AbortSignal) => {
    const version = ++readVersion.current;
    setLoading(true);
    try {
      if (!gatewayId) throw new Error(t("gateway.connections.ownerUnavailable"));
      const next = await listGatewayManagementTargets(apiPort, signal);
      signal.throwIfAborted();
      if (next.local_gateway_id !== gatewayId) throw new Error(t("gateway.connections.ownerMismatch"));
      if (version === readVersion.current) setDirectory(next);
    } finally {
      if (!signal.aborted && version === readVersion.current) setLoading(false);
    }
  }, [apiPort, gatewayId]);
  useEffect(() => {
    const release = onGatewayTokenInvalidated(apiPort, () => {
      scope.abort();
      setDirectory(undefined);
      setError(t("gateway.connections.accessChanged"));
    });
    return () => { release(); scope.abort(); };
  }, [apiPort, scope]);
  useEffect(() => {
    const read = new AbortController();
    const signal = AbortSignal.any([scope.signal, read.signal]);
    setError(undefined);
    if (gatewayId) void load(signal).catch(cause => {
      if (!signal.aborted) { setDirectory(undefined); setError(connectionFailure(cause)); }
    });
    return () => read.abort();
  }, [gatewayId, load, revision, scope]);

  const run = async (target: GatewayManagementTargetDTO, action: () => Promise<void>) => {
    if (scope.signal.aborted) return;
    setBusy(target.gateway_connection_id);
    setError(undefined);
    let failure: unknown;
    try { await action(); }
    catch (cause) { failure = cause; }
    // 原写入已发出，关闭页面不撤销它；仅放弃本页的结果发布。
    if (scope.signal.aborted) return;
    const reads = await Promise.allSettled([onRefresh(), load(scope.signal)]);
    for (const read of reads) if (read.status === "rejected") {
      failure = failure ? `${connectionFailure(failure)}\n${connectionFailure(read.reason)}` : read.reason;
    }
    if (scope.signal.aborted) return;
    if (failure) setError(t("gateway.connections.actionFailure", gatewayId!, target.gateway_id,
      target.gateway_connection_id!, connectionFailure(failure)));
    else setEditing(undefined);
    setBusy(undefined);
  };
  const reconnect = async (target: GatewayManagementTargetDTO) => {
    const accepted = await confirm({ title: t("gateway.control.reconnectTitle"),
      message: t("gateway.connections.reconnectDetail", target.name, target.gateway_id), confirmText: t("gateway.control.reconnect") });
    if (!accepted || scope.signal.aborted) return;
    await run(target, async () => {
      await reconnectRemoteGatewayConnection(apiPort, target.gateway_connection_id!);
      onRemoteGatewayReconnected(target.gateway_connection_id!);
    });
  };
  const remove = async (target: GatewayManagementTargetDTO) => {
    const accepted = await confirm({ title: t("gateway.connections.removeTitle"),
      message: t("gateway.connections.removeDetail", target.name, target.gateway_id),
      confirmText: t("gateway.connections.remove"), danger: true });
    if (!accepted || scope.signal.aborted) return;
    await run(target, async () => { await removeRemoteGatewayConnection(apiPort, target.gateway_connection_id!); });
  };
  const targets = directory?.local_gateway_id === gatewayId
    ? directory.items.filter(target => target.connection_kind === "remote_gateway") : undefined;
  return <section className="gateway-connection-section" aria-labelledby="remote-gateway-connections-title">
    <div className="gateway-connection-section-heading">
      <div><h2 id="remote-gateway-connections-title">{t("gateway.control.remoteGateways")}</h2><p>{t("gateway.connections.ownerDetail", gatewayId ?? t("gateway.connections.ownerUnavailable"))}</p></div>
      {targets ? <span>{t("gateway.control.connectionCount", targets.length)}</span> : null}
    </div>
    {error ? <p className="gateway-console-feedback error" role="alert">{error}</p> : null}
    {loading ? <p role="status">{t("gateway.connections.loading")}</p> : null}
    {targets?.map(target => {
      if (!target.gateway_connection_id || !target.remote_gateway_port || !["config", "manual"].includes(target.source_owner ?? "")) {
        throw new Error(t("gateway.connections.invalidTarget", target.gateway_id));
      }
      const connectionId = target.gateway_connection_id;
      const members = workspaces.filter(workspace => workspace.remote_gateways.some(remote => remote.gateway_connection_id === connectionId));
      const remote = members.flatMap(workspace => workspace.remote_gateways).find(item => item.gateway_connection_id === connectionId);
      const editingName = editing?.connectionId === connectionId;
      return <article key={connectionId} className="gateway-connection-section" aria-label={target.name}>
        <h3>{target.name}</h3>
        <p><code>{target.gateway_id}</code> · <code>{connectionId}</code></p>
        <p>{t(target.source_owner === "config" ? "gateway.connections.configOwner" : "gateway.connections.manualOwner")}</p>
        <p>{target.connection_error ?? t("gateway.control.connected")}</p>
        {remote ? <p>{remote.username}@{remote.host}:{remote.port} → Gateway :{target.remote_gateway_port}</p> : <p>{t("gateway.connections.noWorkspaceProjection")}</p>}
        <details><summary>{t("gateway.control.workspaceCount", members.length)}</summary><ul>{members.map(workspace => <li key={workspace.workspace_id}>{workspace.name} <code>{workspace.workspace_id}</code></li>)}</ul></details>
        <div className="gateway-control-actions">
          <button type="button" disabled={!!busy || loading} onClick={() => void reconnect(target)}>{busy === connectionId ? t("gateway.control.connecting") : t("gateway.control.reconnect")}</button>
          <button type="button" onClick={() => onOpenLogs(connectionId)}>{t("gateway.connections.openLogs")}</button>
          {target.source_owner === "manual" ? <>
            <button type="button" disabled={!!busy || loading} onClick={() => setEditing({ connectionId, name: target.name })}>{t("gateway.connections.editName")}</button>
            <button type="button" className="danger-soft" disabled={!!busy || loading} onClick={() => void remove(target)}>{t("gateway.connections.remove")}</button>
          </> : <button type="button" onClick={() => void onOpenConfiguration().catch(cause => {
            if (!scope.signal.aborted) setError(t("gateway.connections.actionFailure", gatewayId!, target.gateway_id,
              connectionId, connectionFailure(cause)));
          })}>{t("gateway.connections.openConfiguration")}</button>}
        </div>
        {editingName ? <form onSubmit={event => {
          event.preventDefault();
          if (!editing.name.trim()) { setError(t("gateway.connections.nameRequired")); return; }
          void run(target, () => onAddSsh({ connection_id: connectionId, name: editing.name.trim(), remote_gateway_port: target.remote_gateway_port! }));
        }}>
          <p>{t("gateway.connections.editBoundary")}</p>
          <label>{t("gateway.connection.connectionName")}<input autoFocus value={editing.name} disabled={!!busy}
            onChange={event => setEditing({ connectionId, name: event.target.value })} /></label>
          <button type="submit" disabled={!!busy}>{t("gateway.connections.saveName")}</button>
          <button type="button" disabled={!!busy} onClick={() => setEditing(undefined)}>{t("gateway.connections.cancel")}</button>
        </form> : null}
      </article>;
    })}
    {targets?.length === 0 && !loading ? <p>{t("gateway.control.noRemoteConnections")}</p> : null}
  </section>;
}
