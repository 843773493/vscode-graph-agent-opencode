import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { listGatewaySshConnections } from "../../gatewayApi";
import type {
  AddSshGatewayWorkspaceRequest,
  SshConnectionOption,
} from "../../types/backend";
import {
  buildSshWorkspaceRequest,
  INITIAL_SSH_WORKSPACE_FORM,
  type WorkspaceSshFormState,
} from "../../utils/workspaceSshRequest";

interface WorkspaceSshDialogProps {
  open: boolean;
  apiPort: number;
  onClose: () => void;
  onSubmit: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
}

function connectionDescription(connection: SshConnectionOption): string {
  const source = connection.source === "boxteam" ? "BoxTeam 配置" : "~/.ssh/config";
  return `${source} · ${connection.username}@${connection.host}:${connection.port}`;
}

export default function WorkspaceSshDialog({
  open,
  apiPort,
  onClose,
  onSubmit,
}: WorkspaceSshDialogProps) {
  const [form, setForm] = useState<WorkspaceSshFormState>(INITIAL_SSH_WORKSPACE_FORM);
  const [connections, setConnections] = useState<SshConnectionOption[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const selectedConnection = useMemo(
    () => connections.find((connection) => connection.connection_id === connectionId),
    [connectionId, connections],
  );

  const update = (key: keyof WorkspaceSshFormState, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    setError(null);
    setSubmitting(false);
    setConnectionsLoading(true);
    void listGatewaySshConnections(apiPort)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setConnections(result.items);
        const initialConnection = result.items[0];
        if (!initialConnection) {
          setConnectionId("");
          setError(
            "没有可用的 SSH Host。请先在 ~/.ssh/config 中添加远程 BoxTeam 主机。",
          );
          return;
        }
        setConnectionId(initialConnection.connection_id);
      })
      .catch((loadError: unknown) => {
        if (!cancelled) {
          setError(
            `读取 SSH 主机配置失败: ${loadError instanceof Error ? loadError.message : String(loadError)}`,
          );
          setConnections([]);
          setConnectionId("");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setConnectionsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [apiPort, open]);

  if (!open) {
    return null;
  }

  const handleConnectionChange = (nextConnectionId: string) => {
    setConnectionId(nextConnectionId);
  };

  const handleSubmit = async () => {
    setError(null);
    let payload: AddSshGatewayWorkspaceRequest;
    try {
      payload = buildSshWorkspaceRequest(form, selectedConnection);
    } catch (validationError) {
      setError(
        validationError instanceof Error
          ? validationError.message
          : String(validationError),
      );
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit(payload);
      setForm(INITIAL_SSH_WORKSPACE_FORM);
      onClose();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="workspace-dialog-backdrop" role="presentation">
      <section
        className="workspace-dialog workspace-remote-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-ssh-dialog-title"
      >
        <header className="workspace-dialog-header workspace-dialog-heading">
          <div>
            <h2 id="workspace-ssh-dialog-title">连接远程 Gateway</h2>
            <p>通过单个 SSH 隧道连接远端 BoxTeam，由远端 Gateway 发现并管理工作区。</p>
          </div>
          <button
            type="button"
            className="workspace-dialog-icon-button"
            aria-label="关闭"
            onClick={onClose}
            disabled={submitting}
          >
            ×
          </button>
        </header>
        <div className="workspace-dialog-grid">
          <label className="workspace-dialog-wide workspace-connection-field">
            <span>远程 BoxTeam 主机</span>
            <span className="workspace-select-shell">
              <span className="codicon codicon-globe" aria-hidden="true" />
              <select
                autoFocus
                value={connectionId}
                onChange={(event) => handleConnectionChange(event.target.value)}
                disabled={connectionsLoading || submitting}
              >
                {connections.map((connection) => (
                  <option key={connection.connection_id} value={connection.connection_id}>
                    {connection.label} · {connection.username}@{connection.host}
                  </option>
                ))}
              </select>
              <span className="codicon codicon-chevron-down" aria-hidden="true" />
            </span>
            {selectedConnection ? (
              <small>{connectionDescription(selectedConnection)}</small>
            ) : (
              <small>SSH 主机来源：已连接 Gateway 与当前用户 ~/.ssh/config。</small>
            )}
          </label>

          <label className="workspace-dialog-wide">
            <span>远程 Gateway 端口</span>
            <span className="workspace-path-input-shell">
              <span className="codicon codicon-server" aria-hidden="true" />
              <input
                value={form.remoteGatewayPort}
                onChange={(event) => update("remoteGatewayPort", event.target.value)}
                inputMode="numeric"
                placeholder="8014"
              />
            </span>
          </label>

          <label>
            <span>连接名称</span>
            <input
              value={form.name}
              onChange={(event) => update("name", event.target.value)}
              placeholder="可留空"
            />
          </label>
        </div>
        {error ? <div className="workspace-dialog-error">{error}</div> : null}
        <div className="workspace-remote-dialog-hint">
          只转发远端 Gateway 的 loopback 端口；不会直接连接远程 Workspace API、Terminal 或 Browser 服务。
        </div>
        <footer className="workspace-dialog-actions">
          <button type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="workspace-dialog-primary"
            onClick={handleSubmit}
            disabled={submitting || connectionsLoading || !selectedConnection}
          >
            {submitting ? "连接中" : "连接 Gateway"}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
