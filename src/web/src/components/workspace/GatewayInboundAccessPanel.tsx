import { useCallback, useEffect, useState } from "react";
import { listGatewayInboundAccess } from "../../gatewayApi";
import type { GatewayInboundAccessList } from "../../types/backend";

interface GatewayInboundAccessPanelProps {
  apiPort: number;
}

function formatExpiry(value: string): string {
  return new Date(value).toLocaleString();
}

export default function GatewayInboundAccessPanel({
  apiPort,
}: GatewayInboundAccessPanelProps) {
  const [result, setResult] = useState<GatewayInboundAccessList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await listGatewayInboundAccess(apiPort));
    } catch (loadError) {
      setResult(null);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, [apiPort]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section
      className="gateway-managed-panel gateway-inbound-panel"
      aria-labelledby="gateway-inbound-access-title"
    >
      <div className="gateway-managed-heading">
        <div>
          <h2 id="gateway-inbound-access-title">外部 Gateway 接入</h2>
          <p>
            查看已接入本机 Gateway、并可管理本机直接工作区的其他 Gateway。
            远程投影工作区仍只在“路由总览”中显示。
          </p>
        </div>
        <button
          type="button"
          className="gateway-compact-button"
          disabled={loading}
          onClick={() => void load()}
        >
          <span
            className={`codicon codicon-refresh${
              loading ? " codicon-modifier-spin" : ""
            }`}
            aria-hidden="true"
          />
          刷新接入状态
        </button>
      </div>

      {error ? (
        <div className="gateway-console-alert" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <div><strong>读取外部接入失败</strong><span>{error}</span></div>
        </div>
      ) : null}

      {loading ? (
        <div className="gateway-managed-loading">
          <span
            className="codicon codicon-loading codicon-modifier-spin"
            aria-hidden="true"
          />
          正在读取 Federation 接入关系…
        </div>
      ) : result && result.peers.length > 0 ? (
        <>
          <div className="gateway-managed-list-heading">
            <div>
              <strong>已接入本机的 Gateway</strong>
              <code title={result.gateway_id}>{result.gateway_id}</code>
            </div>
            <span>{result.peers.length} 个已授权 Gateway</span>
          </div>
          <div className="gateway-inbound-peer-list">
            {result.peers.map((peer) => (
              <article key={peer.connection_id}>
                <span className="codicon codicon-remote" aria-hidden="true" />
                <div>
                  <strong>{peer.peer_gateway_id}</strong>
                  <code title={peer.connection_id}>{peer.connection_id}</code>
                </div>
                <small>凭据有效至 {formatExpiry(peer.credential_expires_at)}</small>
              </article>
            ))}
          </div>

          <div className="gateway-managed-list-heading gateway-inbound-workspace-heading">
            <div>
              <strong>被外部 Gateway 接入的本机工作区</strong>
              <p>以上 Gateway 均可通过当前 Federation 授权访问这些工作区。</p>
            </div>
            <span>{result.items.length} 个本机工作区</span>
          </div>
          <div className="gateway-managed-workspace-list">
            {result.items.map((workspace) => (
              <article key={workspace.workspace_id}>
                <span
                  className={`gateway-workspace-status ${workspace.status}`}
                  aria-hidden="true"
                />
                <div>
                  <div className="gateway-managed-workspace-title">
                    <h3>{workspace.name}</h3>
                    <span>{workspace.managed ? "本机托管" : "外部后端"}</span>
                    {workspace.system_default ? <span>系统默认</span> : null}
                  </div>
                  <p title={workspace.root_path}>{workspace.root_path}</p>
                  <code title={workspace.workspace_id}>{workspace.workspace_id}</code>
                </div>
                <small>由本机 Gateway 提供</small>
              </article>
            ))}
          </div>
        </>
      ) : (
        <div className="gateway-console-empty">
          <span className="codicon codicon-shield" aria-hidden="true" />
          <strong>当前没有其他 Gateway 接入本机</strong>
          <p>
            本机工作区没有被外部 Gateway 管理。主动连接到其他 Gateway 的远程工作区，
            请在“路由总览”查看。
          </p>
        </div>
      )}
    </section>
  );
}
