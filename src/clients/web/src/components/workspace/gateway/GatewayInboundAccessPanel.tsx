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

interface GatewayInboundAccessPanelProps {
  apiPort: number;
  revision?: number;
  onAddDevice: () => void;
}

export default function GatewayInboundAccessPanel({
  apiPort,
  revision = 0,
  onAddDevice,
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
      setInbound(null);
      setDevices([]);
      setError(errorMessage(loadError));
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
      setError(errorMessage(revokeError));
    } finally {
      setRevokingId(null);
    }
  };

  return (
    <section className="gateway-device-section" aria-labelledby="gateway-external-device-title">
      <div className="gateway-connection-section-heading">
        <div>
          <h2 id="gateway-external-device-title">{t("gateway.inbound.title")}</h2>
          <p>{t("gateway.inbound.description")}</p>
        </div>
        <div className="gateway-section-actions">
          <button type="button" className="gateway-compact-button" onClick={onAddDevice}>
            <span className="codicon codicon-add" aria-hidden="true" />
            {t("gateway.inbound.addDevice")}
          </button>
          <button type="button" className="gateway-compact-button" disabled={loading} onClick={() => void load()}>
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
            {t("gateway.common.refresh")}
          </button>
        </div>
      </div>

      {error ? <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" aria-hidden="true" /><div><strong>{t("gateway.inbound.readFailed")}</strong><span>{error}</span></div></div> : null}

      {loading ? (
        <div className="gateway-connection-state"><span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />{t("gateway.inbound.loading")}</div>
      ) : devices.length > 0 ? (
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
      ) : (
        <div className="gateway-connection-empty">
          <span className="codicon codicon-device-mobile" aria-hidden="true" />
          <strong>{t("gateway.inbound.noDevices")}</strong>
          <p>{t("gateway.inbound.noDevicesDetail")}</p>
        </div>
      )}

      {gatewayPeers.length > 0 ? (
        <div className="gateway-federation-peers">
          <h3>{t("gateway.inbound.peerGateways")}</h3>
          {gatewayPeers.map((peer) => (
            <article key={peer.connection_id}>
              <span className="codicon codicon-remote" aria-hidden="true" />
              <div><strong>{peer.peer_gateway_id}</strong><small>{t("gateway.inbound.credentialExpiresAt", formatLocalDateTime(peer.credential_expires_at))}</small></div>
              <span className="gateway-authorization-status authorized">{t("gateway.inbound.authorized")}</span>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}
