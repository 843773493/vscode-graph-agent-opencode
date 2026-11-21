import { useCallback, useEffect, useMemo, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import {
  listGatewayDeviceConnections,
  listGatewayInboundAccess,
  revokeGatewayDeviceConnection,
} from "../../../gatewayApi";
import type {
  GatewayDeviceConnection,
  GatewayInboundAccessList,
} from "../../../types/backend";
import { formatLocalDateTime } from "../../../utils/format";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import { errorMessage } from "../../../utils/errorMessage";
import { HttpRequestError } from "../../../api/http";

function accessError(error: unknown): string {
  return error instanceof HttpRequestError && error.requestId
    ? `${errorMessage(error)} · request_id=${error.requestId}` : errorMessage(error);
}

interface GatewayInboundAccessPanelProps {
  apiPort: number;
  revision?: number;
}

export default function GatewayInboundAccessPanel({
  apiPort,
  revision = 0,
}: GatewayInboundAccessPanelProps) {
  const confirm = useWarmConfirm();
  const [inbound, setInbound] = useState<GatewayInboundAccessList | null>(null);
  const [devices, setDevices] = useState<GatewayDeviceConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextInbound, nextDevices] = await Promise.all([
        listGatewayInboundAccess(apiPort),
        listGatewayDeviceConnections(apiPort),
      ]);
      setInbound(nextInbound);
      setDevices(nextDevices.items);
    } catch (loadError) {
      setError(accessError(loadError));
    } finally {
      setLoading(false);
    }
  }, [apiPort]);

  useEffect(() => {
    void load();
  }, [load, revision]);

  const gatewayPeers = useMemo(
    () => inbound?.peers.filter((peer) => !peer.peer_gateway_id.startsWith("device:")) ?? [],
    [inbound?.peers],
  );

  const revoke = async (device: GatewayDeviceConnection) => {
    const accepted = await confirm({
      title: t("gateway.inbound.revokeTitle"),
      message: t("gateway.inbound.revokeDetail", device.device_name),
      confirmText: t("gateway.inbound.revoke"),
      danger: true,
    });
    if (!accepted) return;
    setRevokingId(device.connection_id);
    setError(null);
    try {
      const result = await revokeGatewayDeviceConnection(apiPort, device.connection_id);
      setDevices(result.items);
      await load();
    } catch (revokeError) {
      await load();
      setError((readError) => [accessError(revokeError), readError].filter(Boolean).join("\n"));
    } finally {
      setRevokingId(null);
    }
  };

  return (
    <section className="gateway-device-section panel-feedback-host" aria-busy={loading} aria-labelledby="gateway-external-device-title">
      <div className="gateway-connection-section-heading">
        <div>
          <h2 id="gateway-external-device-title">{t("gateway.inbound.title")}</h2>
          <p>{t("gateway.inbound.description")}</p>
        </div>

      </div>

      {error ? <div className="panel-feedback" role="alert"><span className="codicon codicon-error" aria-hidden="true" /><div><strong>{t("gateway.inbound.operationFailed")}</strong><span>{error}</span></div></div> : null}

      {inbound ? <div className="gateway-federation-peers" aria-label={t("gateway.inbound.scope")}>
        <h3>{t("gateway.inbound.owner")}</h3>
        <code>{inbound.gateway_id}</code>
        <p>{t("gateway.inbound.localOnly")}</p>
        <h3>{t("gateway.inbound.workspaces")}</h3>
        <p>{t("gateway.inbound.workspaceScope")}</p>
        {inbound.items.length ? <ul>{inbound.items.map((workspace) => <li key={workspace.workspace_id}>
          <strong>{workspace.name}</strong> <code>{workspace.workspace_id}</code>
        </li>)}</ul> : <p>{t("gateway.inbound.noAuthorizedWorkspaces")}</p>}
      </div> : null}

      {devices.length > 0 ? (
        <div className="gateway-device-list">
          {devices.map((device) => (
            <article key={device.connection_id}>
              <span className="gateway-device-icon"><span className="codicon codicon-device-mobile" aria-hidden="true" /></span>
              <div className="gateway-device-copy">
                <div><strong>{device.device_name}</strong><span className={`gateway-authorization-status ${device.status}`}>{device.status === "authorized" ? t("gateway.inbound.authorized") : t("gateway.inbound.expired")}</span></div>
                <small>{t("gateway.inbound.credentialExpiresAt", formatLocalDateTime(device.credential_expires_at))}</small>
                <code title={device.connection_id}>{device.connection_id}</code>
              </div>
              <button type="button" className="danger" disabled={revokingId === device.connection_id} onClick={() => void revoke(device)}>
                {revokingId === device.connection_id ? t("gateway.inbound.revoking") : t("gateway.inbound.revoke")}
              </button>
            </article>
          ))}
        </div>
      ) : !loading && !error ? (
        <div className="gateway-connection-empty">
          <span className="codicon codicon-device-mobile" aria-hidden="true" />
          <strong>{t("gateway.inbound.noDevices")}</strong>
          <p>{t("gateway.inbound.noDevicesDetail")}</p>
        </div>
      ) : null}

      {gatewayPeers.length > 0 ? (
        <div className="gateway-federation-peers">
          <h3>{t("gateway.inbound.peerGateways")}</h3>
          {gatewayPeers.map((peer) => (
            <article key={peer.connection_id}>
              <span className="codicon codicon-remote" aria-hidden="true" />
              <div><strong>{peer.peer_gateway_id}</strong><small>{peer.connection_id}</small><small>{t("gateway.inbound.credentialExpiresAt", formatLocalDateTime(peer.credential_expires_at))}</small></div>
              <span className="gateway-authorization-status authorized">{t("gateway.inbound.authorized")}</span>
            </article>
          ))}
        </div>
      ) : null}
      <div className="gateway-federation-peers" aria-label={t("gateway.inbound.credentialManagement")}>
        <h3>{t("gateway.inbound.credentialManagement")}</h3>
        <p>{t("gateway.inbound.deviceCredentialManagement")}</p>
        <p>{t("gateway.inbound.otherCredentialManagement")}</p>
      </div>
    </section>
  );
}
