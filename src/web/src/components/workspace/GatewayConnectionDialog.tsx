import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  createGatewayDeviceConnection,
  listGatewayDeviceAccessAddresses,
  listGatewaySshConnections,
} from "../../gatewayApi";
import type {
  AddSshGatewayWorkspaceRequest,
  CreatedGatewayDeviceConnection,
  GatewayDeviceAccessAddress,
  SshConnectionOption,
} from "../../types/backend";

type ConnectionPage = "ssh-select" | "ssh-manual" | "external-device" | "device-info";
type ConnectionDialogMode = "ssh" | "external-device";

interface GatewayConnectionDialogProps {
  open: boolean;
  mode: ConnectionDialogMode;
  apiPort: number;
  onClose: () => void;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
  onDeviceConnectionsChanged: () => void;
}

export interface ManualSshForm {
  name: string;
  host: string;
  port: string;
  username: string;
  privateKeyPath: string;
  remoteGatewayPort: string;
}

const INITIAL_MANUAL_SSH_FORM: ManualSshForm = {
  name: "",
  host: "",
  port: "22",
  username: "",
  privateKeyPath: "~/.ssh/id_ed25519",
  remoteGatewayPort: "8014",
};

export function buildSelectedSshConnectionRequest(
  connection: SshConnectionOption,
): AddSshGatewayWorkspaceRequest {
  if (connection.source === "boxteam" && connection.workspace_id) {
    return {
      connection_workspace_id: connection.workspace_id,
      remote_gateway_port: 8014,
    };
  }
  if (connection.source === "ssh_config" && connection.ssh_config_host) {
    return {
      ssh_config_host: connection.ssh_config_host,
      remote_gateway_port: 8014,
    };
  }
  throw new Error(`远程连接 ${connection.label} 缺少可用的 SSH 配置`);
}

function parsePort(value: string, label: string): number {
  const port = Number(value.trim());
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${label}必须是 1-65535 的整数`);
  }
  return port;
}

export function buildManualSshConnectionRequest(
  form: ManualSshForm,
): AddSshGatewayWorkspaceRequest {
  const host = form.host.trim();
  const username = form.username.trim();
  const privateKeyPath = form.privateKeyPath.trim();
  if (!host) throw new Error("SSH 主机不能为空");
  if (!username) throw new Error("SSH 用户名不能为空");
  if (!privateKeyPath) throw new Error("SSH 私钥路径不能为空");
  return {
    name: form.name.trim() || null,
    host,
    port: parsePort(form.port, "SSH 端口"),
    username,
    private_key_path: privateKeyPath,
    remote_gateway_port: parsePort(form.remoteGatewayPort, "远程 Gateway 端口"),
  };
}

function connectionInfoText(result: CreatedGatewayDeviceConnection): string {
  return JSON.stringify(
    {
      gateway_url: result.connection_info.gateway_url,
      federation_token: result.connection_info.federation_token,
      request_header: result.connection_info.request_header,
      manifest_url: result.connection_info.manifest_url,
      workspaces_url: result.connection_info.workspaces_url,
      expires_at: result.connection.credential_expires_at,
    },
    null,
    2,
  );
}

export default function GatewayConnectionDialog({
  open,
  mode,
  apiPort,
  onClose,
  onAddSsh,
  onDeviceConnectionsChanged,
}: GatewayConnectionDialogProps) {
  const [page, setPage] = useState<ConnectionPage>("ssh-select");
  const [connections, setConnections] = useState<SshConnectionOption[]>([]);
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [manualForm, setManualForm] = useState<ManualSshForm>(INITIAL_MANUAL_SSH_FORM);
  const [deviceName, setDeviceName] = useState("我的手机");
  const [gatewayUrl, setGatewayUrl] = useState("");
  const [accessAddresses, setAccessAddresses] = useState<GatewayDeviceAccessAddress[]>([]);
  const [manualAddress, setManualAddress] = useState(false);
  const [createdDevice, setCreatedDevice] = useState<CreatedGatewayDeviceConnection | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadSshConnections = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listGatewaySshConnections(apiPort);
      setConnections(result.items);
      setSelectedConnectionId((current) =>
        result.items.some((item) => item.connection_id === current) ? current : null,
      );
    } catch (loadError) {
      setConnections([]);
      setSelectedConnectionId(null);
      setError(
        `读取 ~/.ssh/config 失败：${loadError instanceof Error ? loadError.message : String(loadError)}`,
      );
    } finally {
      setLoading(false);
    }
  }, [apiPort]);

  const loadDeviceAddresses = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listGatewayDeviceAccessAddresses(apiPort);
      setAccessAddresses(result.items);
      setGatewayUrl(
        result.items.find((item) => !item.is_loopback)?.url
          ?? result.items[0]?.url
          ?? window.location.origin,
      );
    } catch (loadError) {
      setAccessAddresses([]);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, [apiPort]);

  useEffect(() => {
    if (!open) return;
    const initialPage = mode === "ssh" ? "ssh-select" : "external-device";
    setPage(initialPage);
    setSelectedConnectionId(null);
    setManualForm(INITIAL_MANUAL_SSH_FORM);
    setDeviceName("我的手机");
    setGatewayUrl("");
    setAccessAddresses([]);
    setManualAddress(false);
    setCreatedDevice(null);
    setError(null);
    setCopied(false);
    if (mode === "ssh") {
      void loadSshConnections();
    } else {
      void loadDeviceAddresses();
    }
  }, [loadDeviceAddresses, loadSshConnections, mode, open]);

  const closeOrBack = () => {
    if (page === "ssh-manual") {
      setPage("ssh-select");
      setError(null);
      return;
    }
    if (page === "device-info") {
      setPage("external-device");
      setCreatedDevice(null);
      setError(null);
      return;
    }
    onClose();
  };

  const addSelectedConnection = async () => {
    const selected = connections.find(
      (connection) => connection.connection_id === selectedConnectionId,
    );
    if (!selected) {
      setError("请选择一个 SSH 连接");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onAddSsh(buildSelectedSshConnectionRequest(selected));
      onClose();
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : String(connectError));
    } finally {
      setSubmitting(false);
    }
  };

  const addManualConnection = async () => {
    let payload: AddSshGatewayWorkspaceRequest;
    try {
      payload = buildManualSshConnectionRequest(manualForm);
    } catch (validationError) {
      setError(validationError instanceof Error ? validationError.message : String(validationError));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onAddSsh(payload);
      onClose();
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : String(connectError));
    } finally {
      setSubmitting(false);
    }
  };

  const createDevice = async () => {
    const normalizedName = deviceName.trim();
    if (!normalizedName) {
      setError("请输入设备名称");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await createGatewayDeviceConnection(apiPort, {
        device_name: normalizedName,
        gateway_url: gatewayUrl.trim(),
      });
      setCreatedDevice(result);
      setPage("device-info");
      onDeviceConnectionsChanged();
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : String(createError));
    } finally {
      setSubmitting(false);
    }
  };

  const copyConnectionInfo = async () => {
    if (!createdDevice) return;
    if (!navigator.clipboard) {
      setError("当前浏览器不支持剪贴板，请手动复制连接信息");
      return;
    }
    try {
      await navigator.clipboard.writeText(connectionInfoText(createdDevice));
      setCopied(true);
    } catch (copyError) {
      setError(copyError instanceof Error ? copyError.message : String(copyError));
    }
  };

  const updateManualForm = (field: keyof ManualSshForm, value: string) => {
    setManualForm((current) => ({ ...current, [field]: value }));
  };

  if (!open) return null;

  const isLoopbackAddress = /(?:localhost|127\.0\.0\.1)(?::\d+)?$/i.test(
    (() => {
      try {
        return new URL(gatewayUrl).host;
      } catch {
        return "";
      }
    })(),
  );

  const title = page === "ssh-select"
    ? "添加 SSH 连接"
    : page === "ssh-manual"
      ? "手动添加 SSH 连接"
      : page === "external-device"
        ? "连接外部设备"
        : "设备连接信息";

  return createPortal(
    <div className="gateway-connection-overlay" role="presentation">
      <button
        type="button"
        className="gateway-connection-backdrop"
        aria-label={`关闭${title}`}
        onClick={onClose}
      />
      <section
        className="gateway-connection-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gateway-connection-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !submitting) {
            event.preventDefault();
            closeOrBack();
          }
        }}
      >
        <header>
          {page === "ssh-manual" || page === "device-info" ? (
            <button type="button" aria-label="返回" onClick={closeOrBack} disabled={submitting}>
              <span className="codicon codicon-arrow-left" aria-hidden="true" />
            </button>
          ) : (
            <span className={`codicon codicon-${page === "ssh-select" ? "remote" : "device-mobile"}`} aria-hidden="true" />
          )}
          <div>
            <h2 id="gateway-connection-dialog-title">{title}</h2>
            <p>
              {page === "ssh-select"
                ? "自动读取当前用户 ~/.ssh/config，也可以手动填写连接信息。"
                : page === "ssh-manual"
                  ? "填写一台运行 BoxTeam Gateway 的 SSH 主机。"
                  : page === "external-device"
                    ? "生成一份 30 天有效的设备凭据，并发送到手机或其他设备。"
                    : "凭据只显示这一次，请复制后发送到目标设备。"}
            </p>
          </div>
          <button type="button" aria-label="关闭" onClick={onClose} disabled={submitting}>
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </header>

        {error ? <div className="gateway-connection-error" role="alert">{error}</div> : null}

        <div className="gateway-connection-dialog-content" aria-busy={loading || submitting}>
          {page === "ssh-select" ? (
            <div className="gateway-ssh-picker">
              <div className="gateway-ssh-host-list" role="radiogroup" aria-label="SSH 连接">
                {loading ? (
                  <div className="gateway-connection-state">
                    <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
                    正在读取 ~/.ssh/config…
                  </div>
                ) : null}
                {!loading && connections.length === 0 ? (
                  <div className="gateway-connection-state">
                    <span className="codicon codicon-remote" aria-hidden="true" />
                    <strong>没有发现 SSH 主机</strong>
                    <small>请检查 ~/.ssh/config，或使用手动添加。</small>
                  </div>
                ) : null}
                {!loading && connections.map((connection) => {
                  const selected = connection.connection_id === selectedConnectionId;
                  return (
                    <button
                      key={connection.connection_id}
                      type="button"
                      className={`gateway-connection-host-row${selected ? " selected" : ""}`}
                      role="radio"
                      aria-checked={selected}
                      disabled={submitting}
                      onClick={() => {
                        setSelectedConnectionId(connection.connection_id);
                        setError(null);
                      }}
                    >
                      <span className="codicon codicon-device-desktop" aria-hidden="true" />
                      <span>
                        <strong>{connection.label}</strong>
                        <small>{connection.host}{connection.port === 22 ? "" : `:${connection.port}`}</small>
                      </span>
                      <span className={`codicon codicon-${selected ? "pass-filled" : "circle-large-outline"}`} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
              <footer className="gateway-ssh-picker-footer">
                <div>
                  <button type="button" disabled={loading || submitting} onClick={() => void loadSshConnections()}>
                    <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
                    刷新
                  </button>
                  <button type="button" disabled={submitting} onClick={() => { setPage("ssh-manual"); setError(null); }}>
                    <span className="codicon codicon-edit" aria-hidden="true" />
                    手动添加
                  </button>
                </div>
                <button
                  type="button"
                  className="primary"
                  disabled={loading || submitting || selectedConnectionId === null}
                  onClick={() => void addSelectedConnection()}
                >
                  {submitting ? "添加中…" : "添加"}
                </button>
              </footer>
            </div>
          ) : null}

          {page === "ssh-manual" ? (
            <form className="gateway-manual-ssh-form" onSubmit={(event) => { event.preventDefault(); void addManualConnection(); }}>
              <label className="wide"><span>连接名称</span><input autoFocus value={manualForm.name} onChange={(event) => updateManualForm("name", event.target.value)} placeholder="可留空" disabled={submitting} /></label>
              <label className="wide"><span>SSH 主机</span><input value={manualForm.host} onChange={(event) => updateManualForm("host", event.target.value)} placeholder="例如 192.168.1.20" disabled={submitting} /></label>
              <label><span>SSH 端口</span><input value={manualForm.port} onChange={(event) => updateManualForm("port", event.target.value)} inputMode="numeric" disabled={submitting} /></label>
              <label><span>用户名</span><input value={manualForm.username} onChange={(event) => updateManualForm("username", event.target.value)} autoComplete="username" disabled={submitting} /></label>
              <label className="wide"><span>私钥路径</span><input value={manualForm.privateKeyPath} onChange={(event) => updateManualForm("privateKeyPath", event.target.value)} placeholder="~/.ssh/id_ed25519" disabled={submitting} /></label>
              <label className="wide"><span>远程 Gateway 端口</span><input value={manualForm.remoteGatewayPort} onChange={(event) => updateManualForm("remoteGatewayPort", event.target.value)} inputMode="numeric" disabled={submitting} /></label>
              <footer>
                <button type="button" onClick={closeOrBack} disabled={submitting}>取消</button>
                <button type="submit" className="primary" disabled={submitting}>{submitting ? "添加中…" : "添加"}</button>
              </footer>
            </form>
          ) : null}

          {page === "external-device" ? (
            <div className="gateway-device-form">
              <label>
                <span>设备名称</span>
                <input autoFocus value={deviceName} onChange={(event) => setDeviceName(event.target.value)} disabled={submitting} />
              </label>
              <fieldset className="gateway-device-addresses">
                <legend>设备访问地址</legend>
                {accessAddresses.map((address) => (
                  <button
                    key={address.url}
                    type="button"
                    className={!manualAddress && gatewayUrl === address.url ? "selected" : undefined}
                    onClick={() => { setGatewayUrl(address.url); setManualAddress(false); }}
                    disabled={submitting}
                  >
                    <span className={`codicon codicon-${address.is_loopback ? "device-desktop" : "radio-tower"}`} aria-hidden="true" />
                    <span><strong>{address.label}</strong><small>{address.url}{address.is_loopback ? " · 仅本机可用" : " · 请确保设备与电脑网络互通"}</small></span>
                    <span className={`codicon codicon-${!manualAddress && gatewayUrl === address.url ? "pass-filled" : "circle-large-outline"}`} aria-hidden="true" />
                  </button>
                ))}
                <button type="button" className={manualAddress ? "selected" : undefined} onClick={() => setManualAddress(true)} disabled={submitting}>
                  <span className="codicon codicon-edit" aria-hidden="true" />
                  <span><strong>手动地址</strong><small>仅在以上候选地址无法访问时使用</small></span>
                  <span className={`codicon codicon-${manualAddress ? "pass-filled" : "circle-large-outline"}`} aria-hidden="true" />
                </button>
              </fieldset>
              {manualAddress ? (
                <label><span>手动输入访问地址</span><input value={gatewayUrl} onChange={(event) => setGatewayUrl(event.target.value)} disabled={submitting} placeholder="例如 http://192.168.1.20:8011" /></label>
              ) : null}
              {isLoopbackAddress ? (
                <div className="gateway-device-address-warning" role="status"><span className="codicon codicon-warning" aria-hidden="true" />外部设备无法访问 localhost。请选择局域网地址或手动输入可访问地址。</div>
              ) : null}
              <div className="gateway-device-permission-note"><span className="codicon codicon-shield" aria-hidden="true" /><p><strong>访问范围</strong><small>该设备可读取本 Gateway 直接管理的工作区并通过 Gateway 转发请求；可随时在连接列表撤销。</small></p></div>
              <button type="button" className="primary" disabled={loading || submitting || !deviceName.trim() || !gatewayUrl.trim()} onClick={() => void createDevice()}>{submitting ? "正在生成…" : "生成连接信息"}</button>
            </div>
          ) : null}

          {page === "device-info" && createdDevice ? (
            <div className="gateway-device-info">
              <div className="gateway-device-created"><span className="codicon codicon-pass-filled" aria-hidden="true" /><p><strong>{createdDevice.connection.device_name} 已授权</strong><small>凭据有效至 {new Date(createdDevice.connection.credential_expires_at).toLocaleString()}</small></p></div>
              <pre>{connectionInfoText(createdDevice)}</pre>
              <button type="button" className="primary" onClick={() => void copyConnectionInfo()}><span className={`codicon codicon-${copied ? "pass" : "copy"}`} aria-hidden="true" />{copied ? "已复制" : "复制连接信息"}</button>
            </div>
          ) : null}
        </div>
      </section>
    </div>,
    document.body,
  );
}
