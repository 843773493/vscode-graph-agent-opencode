import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { useCallback, useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { t } from "../../../../../shared/core/i18n/index.js";
import { OverlayRootContext } from "../../overlays/OverlayRootContext";
import {
  createGatewayDeviceConnection,
  listGatewayDeviceAccessAddresses,
  listGatewaySshConnections,
} from "../../../gatewayApi";
import type {
  AddRemoteGatewayConnectionRequest,
  CreatedGatewayDeviceConnection,
  GatewayDeviceAccessAddress,
  SshConnectionOption,
} from "../../../types/backend";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { errorMessage } from "../../../utils/errorMessage";
import { formatLocalDateTime } from "../../../utils/format";
import { parsePortLiteral } from "../portLiteral";

type ConnectionPage = "ssh-select" | "ssh-manual" | "external-device" | "device-info";
type ConnectionDialogMode = "ssh" | "external-device";

interface GatewayConnectionDialogProps {
  open: boolean;
  mode: ConnectionDialogMode;
  apiPort: number;
  onClose: () => void;
  onAddRemoteGatewayConnection: (payload: AddRemoteGatewayConnectionRequest) => Promise<void>;
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
): AddRemoteGatewayConnectionRequest {
  if (connection.source === "boxteam") {
    return {
      connection_id: connection.connection_id,
      remote_gateway_port: 8014,
    };
  }
  if (connection.source === "ssh_config" && connection.ssh_config_host) {
    return {
      ssh_config_host: connection.ssh_config_host,
      remote_gateway_port: 8014,
    };
  }
  throw new Error(t("gateway.connection.missingSshConfiguration", connection.label));
}

export function buildManualSshConnectionRequest(
  form: ManualSshForm,
): AddRemoteGatewayConnectionRequest {
  const host = form.host.trim();
  const username = form.username.trim();
  const privateKeyPath = form.privateKeyPath.trim();
  if (!host) throw new Error(t("gateway.connection.hostRequired"));
  if (!username) throw new Error(t("gateway.connection.usernameRequired"));
  if (!privateKeyPath) throw new Error(t("gateway.connection.privateKeyPathRequired"));
  return {
    name: form.name.trim() || null,
    host,
    port: parsePortLiteral(form.port, t("gateway.connection.sshPort")),
    username,
    private_key_path: privateKeyPath,
    remote_gateway_port: parsePortLiteral(form.remoteGatewayPort, t("gateway.connection.remoteGatewayPort")),
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
  onAddRemoteGatewayConnection,
  onDeviceConnectionsChanged,
}: GatewayConnectionDialogProps) {
  const overlayRoot = useContext(OverlayRootContext);
  const [page, setPage] = useState<ConnectionPage>("ssh-select");
  const [connections, setConnections] = useState<SshConnectionOption[]>([]);
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [manualForm, setManualForm] = useState<ManualSshForm>(INITIAL_MANUAL_SSH_FORM);
  const [deviceName, setDeviceName] = useState(() => t("gateway.connection.defaultDeviceName"));
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
        t("gateway.connection.readSshConfigFailed", errorMessage(loadError)),
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
      setError(errorMessage(loadError));
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
    setDeviceName(t("gateway.connection.defaultDeviceName"));
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
      setError(t("gateway.connection.selectSshConnection"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onAddRemoteGatewayConnection(buildSelectedSshConnectionRequest(selected));
      onClose();
    } catch (connectError) {
      setError(errorMessage(connectError));
    } finally {
      setSubmitting(false);
    }
  };

  const addManualConnection = async () => {
    let payload: AddRemoteGatewayConnectionRequest;
    try {
      payload = buildManualSshConnectionRequest(manualForm);
    } catch (validationError) {
      setError(errorMessage(validationError));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onAddRemoteGatewayConnection(payload);
      onClose();
    } catch (connectError) {
      setError(errorMessage(connectError));
    } finally {
      setSubmitting(false);
    }
  };

  const createDevice = async () => {
    const normalizedName = deviceName.trim();
    if (!normalizedName) {
      setError(t("gateway.connection.deviceNameRequired"));
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
      setError(errorMessage(createError));
    } finally {
      setSubmitting(false);
    }
  };

  const copyConnectionInfo = async () => {
    if (!createdDevice) return;
    try {
      await copyTextToClipboard(connectionInfoText(createdDevice));
      setCopied(true);
    } catch (copyError) {
      setError(errorMessage(copyError));
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
    ? t("gateway.connection.addSshTitle")
    : page === "ssh-manual"
      ? t("gateway.connection.manualSshTitle")
      : page === "external-device"
        ? t("gateway.connection.externalDeviceTitle")
        : t("gateway.connection.deviceInfoTitle");

  return createPortal(
    <div className="gateway-connection-overlay" role="presentation">
      <button
        type="button"
        className="gateway-connection-backdrop"
        aria-label={t("gateway.connection.closeDialog", title)}
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
            <button type="button" aria-label={t("gateway.connection.back")} onClick={closeOrBack} disabled={submitting}>
              <span className="codicon codicon-arrow-left" aria-hidden="true" />
            </button>
          ) : (
            <span className={`codicon codicon-${page === "ssh-select" ? "remote" : "device-mobile"}`} aria-hidden="true" />
          )}
          <div>
            <h2 id="gateway-connection-dialog-title">{title}</h2>
            <p>
              {page === "ssh-select"
                ? t("gateway.connection.sshSelectDescription")
                : page === "ssh-manual"
                  ? t("gateway.connection.sshManualDescription")
                  : page === "external-device"
                    ? t("gateway.connection.externalDeviceDescription")
                    : t("gateway.connection.deviceInfoDescription")}
            </p>
          </div>
          <button type="button" aria-label={t("gateway.connection.close")} onClick={onClose} disabled={submitting}>
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </header>

        {error ? <div className="gateway-connection-error failure-summary" role="alert">{failureSummary(error)}</div> : null}

        <div className="gateway-connection-dialog-content" aria-busy={loading || submitting}>
          {page === "ssh-select" ? (
            <div className="gateway-ssh-picker">
              <div className="gateway-ssh-host-list" role="radiogroup" aria-label={t("gateway.connection.sshConnections")}>

                {!loading && connections.length === 0 ? (
                  <div className="gateway-connection-state">
                    <span className="codicon codicon-remote" aria-hidden="true" />
                    <strong>{t("gateway.connection.noSshHosts")}</strong>
                    <small>{t("gateway.connection.checkSshConfigOrAddManually")}</small>
                  </div>
                ) : null}
                {connections.map((connection) => {
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
                    {t("gateway.connection.refresh")}
                  </button>
                  <button type="button" disabled={submitting} onClick={() => { setPage("ssh-manual"); setError(null); }}>
                    <span className="codicon codicon-edit" aria-hidden="true" />
                    {t("gateway.connection.addManually")}
                  </button>
                </div>
                <button
                  type="button"
                  className="primary"
                  disabled={loading || submitting || selectedConnectionId === null}
                  onClick={() => void addSelectedConnection()}
                >
                  {submitting ? t("gateway.connection.adding") : t("gateway.connection.add")}
                </button>
              </footer>
            </div>
          ) : null}

          {page === "ssh-manual" ? (
            <form className="gateway-manual-ssh-form" onSubmit={(event) => { event.preventDefault(); void addManualConnection(); }}>
              <label className="wide"><span>{t("gateway.connection.connectionName")}</span><input autoFocus value={manualForm.name} onChange={(event) => updateManualForm("name", event.target.value)} placeholder={t("gateway.connection.optional")} disabled={submitting} /></label>
              <label className="wide"><span>{t("gateway.connection.sshHost")}</span><input value={manualForm.host} onChange={(event) => updateManualForm("host", event.target.value)} placeholder={t("gateway.connection.hostExample")} disabled={submitting} /></label>
              <label><span>{t("gateway.connection.sshPort")}</span><input value={manualForm.port} onChange={(event) => updateManualForm("port", event.target.value)} inputMode="numeric" disabled={submitting} /></label>
              <label><span>{t("gateway.connection.username")}</span><input value={manualForm.username} onChange={(event) => updateManualForm("username", event.target.value)} autoComplete="username" disabled={submitting} /></label>
              <label className="wide"><span>{t("gateway.connection.privateKeyPath")}</span><input value={manualForm.privateKeyPath} onChange={(event) => updateManualForm("privateKeyPath", event.target.value)} placeholder="~/.ssh/id_ed25519" disabled={submitting} /></label>
              <label className="wide"><span>{t("gateway.connection.remoteGatewayPort")}</span><input value={manualForm.remoteGatewayPort} onChange={(event) => updateManualForm("remoteGatewayPort", event.target.value)} inputMode="numeric" disabled={submitting} /></label>
              <footer>
                <button type="button" onClick={closeOrBack} disabled={submitting}>{t("gateway.connection.cancel")}</button>
                <button type="submit" className="primary" disabled={submitting}>{submitting ? t("gateway.connection.adding") : t("gateway.connection.add")}</button>
              </footer>
            </form>
          ) : null}

          {page === "external-device" ? (
            <div className="gateway-device-form">
              <label>
                <span>{t("gateway.connection.deviceName")}</span>
                <input autoFocus value={deviceName} onChange={(event) => setDeviceName(event.target.value)} disabled={submitting} />
              </label>
              <fieldset className="gateway-device-addresses">
                <legend>{t("gateway.connection.deviceAccessAddresses")}</legend>
                {accessAddresses.map((address) => (
                  <button
                    key={address.url}
                    type="button"
                    className={!manualAddress && gatewayUrl === address.url ? "selected" : undefined}
                    onClick={() => { setGatewayUrl(address.url); setManualAddress(false); }}
                    disabled={submitting}
                  >
                    <span className={`codicon codicon-${address.is_loopback ? "device-desktop" : "radio-tower"}`} aria-hidden="true" />
                    <span><strong>{address.label}</strong><small>{address.url}{address.is_loopback ? ` · ${t("gateway.connection.localOnly")}` : ` · ${t("gateway.connection.ensureNetworkReachable")}`}</small></span>
                    <span className={`codicon codicon-${!manualAddress && gatewayUrl === address.url ? "pass-filled" : "circle-large-outline"}`} aria-hidden="true" />
                  </button>
                ))}
                <button type="button" className={manualAddress ? "selected" : undefined} onClick={() => setManualAddress(true)} disabled={submitting}>
                  <span className="codicon codicon-edit" aria-hidden="true" />
                  <span><strong>{t("gateway.connection.manualAddress")}</strong><small>{t("gateway.connection.manualAddressHint")}</small></span>
                  <span className={`codicon codicon-${manualAddress ? "pass-filled" : "circle-large-outline"}`} aria-hidden="true" />
                </button>
              </fieldset>
              {manualAddress ? (
                <label><span>{t("gateway.connection.enterAccessAddress")}</span><input value={gatewayUrl} onChange={(event) => setGatewayUrl(event.target.value)} disabled={submitting} placeholder={t("gateway.connection.addressExample")} /></label>
              ) : null}
              {isLoopbackAddress ? (
                <div className="gateway-device-address-warning" role="status"><span className="codicon codicon-warning" aria-hidden="true" />{t("gateway.connection.loopbackWarning")}</div>
              ) : null}
              <div className="gateway-device-permission-note"><span className="codicon codicon-shield" aria-hidden="true" /><p><strong>{t("gateway.connection.accessScope")}</strong><small>{t("gateway.connection.accessScopeDetail")}</small></p></div>
              <button type="button" className="primary" disabled={loading || submitting || !deviceName.trim() || !gatewayUrl.trim()} onClick={() => void createDevice()}>{submitting ? t("gateway.connection.generating") : t("gateway.connection.generateConnectionInfo")}</button>
            </div>
          ) : null}

          {page === "device-info" && createdDevice ? (
            <div className="gateway-device-info">
              <div className="gateway-device-created"><span className="codicon codicon-pass-filled" aria-hidden="true" /><p><strong>{t("gateway.connection.deviceAuthorized", createdDevice.connection.device_name)}</strong><small>{t("gateway.connection.credentialExpiresAt", formatLocalDateTime(createdDevice.connection.credential_expires_at))}</small></p></div>
              <pre>{connectionInfoText(createdDevice)}</pre>
              <button type="button" className="primary" onClick={() => void copyConnectionInfo()}><span className={`codicon codicon-${copied ? "pass" : "copy"}`} aria-hidden="true" />{copied ? t("gateway.connection.copied") : t("gateway.connection.copyConnectionInfo")}</button>
            </div>
          ) : null}
        </div>
      </section>
    </div>,
    overlayRoot ?? document.body,
  );
}
