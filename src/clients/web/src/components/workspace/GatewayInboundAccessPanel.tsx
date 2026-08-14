import { useCallback, useEffect, useMemo, useState } from "react";
import {
  listGatewayDeviceConnections,
  listGatewayInboundAccess,
  revokeGatewayDeviceConnection,
} from "../../gatewayApi";
import type {
  GatewayDeviceConnection,
  GatewayInboundAccessList,
} from "../../types/backend";
import { useWarmConfirm } from "../WarmConfirmProvider";

interface GatewayInboundAccessPanelProps {
  apiPort: number;
  revision?: number;
  onAddDevice: () => void;
}

function formatExpiry(value: string): string {
  return new Date(value).toLocaleString();
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
      setError(loadError instanceof Error ? loadError.message : String(loadError));
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
      title: "撤销外部设备连接",
      message: `撤销“${device.device_name}”的 Gateway 访问权限？\n\n撤销后，该设备持有的 Federation 凭据会立即失效。`,
      confirmText: "撤销连接",
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
      setError(revokeError instanceof Error ? revokeError.message : String(revokeError));
    } finally {
      setRevokingId(null);
    }
  };

  return (
    <section className="gateway-device-section" aria-labelledby="gateway-external-device-title">
      <div className="gateway-connection-section-heading">
        <div>
          <h2 id="gateway-external-device-title">手机与外部设备</h2>
          <p>通过可撤销的 Federation 凭据访问本 Gateway。状态表示授权有效性，不代表设备当前在线。</p>
        </div>
        <div className="gateway-section-actions">
          <button type="button" className="gateway-compact-button" onClick={onAddDevice}>
            <span className="codicon codicon-add" aria-hidden="true" />
            添加设备
          </button>
          <button type="button" className="gateway-compact-button" disabled={loading} onClick={() => void load()}>
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
            刷新
          </button>
        </div>
      </div>

      {error ? <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" aria-hidden="true" /><div><strong>读取外部设备失败</strong><span>{error}</span></div></div> : null}

      {loading ? (
        <div className="gateway-connection-state"><span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />正在读取授权连接…</div>
      ) : devices.length > 0 ? (
        <div className="gateway-device-list">
          {devices.map((device) => (
            <article key={device.connection_id}>
              <span className="gateway-device-icon"><span className="codicon codicon-device-mobile" aria-hidden="true" /></span>
              <div className="gateway-device-copy">
                <div><strong>{device.device_name}</strong><span className={`gateway-authorization-status ${device.status}`}>{device.status === "authorized" ? "已授权" : "已过期"}</span></div>
                <small>凭据有效至 {formatExpiry(device.credential_expires_at)}</small>
                <code title={device.connection_id}>{device.connection_id}</code>
              </div>
              <button type="button" className="danger" disabled={revokingId === device.connection_id} onClick={() => void revoke(device)}>
                {revokingId === device.connection_id ? "正在撤销…" : "撤销"}
              </button>
            </article>
          ))}
        </div>
      ) : (
        <div className="gateway-connection-empty">
          <span className="codicon codicon-device-mobile" aria-hidden="true" />
          <strong>尚未授权外部设备</strong>
          <p>点击“添加设备”生成可撤销的连接信息。</p>
        </div>
      )}

      {gatewayPeers.length > 0 ? (
        <div className="gateway-federation-peers">
          <h3>接入本机的其他 Gateway</h3>
          {gatewayPeers.map((peer) => (
            <article key={peer.connection_id}>
              <span className="codicon codicon-remote" aria-hidden="true" />
              <div><strong>{peer.peer_gateway_id}</strong><small>授权有效至 {formatExpiry(peer.credential_expires_at)}</small></div>
              <span className="gateway-authorization-status authorized">已授权</span>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}
