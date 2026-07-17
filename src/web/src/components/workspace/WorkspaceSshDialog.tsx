import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  browseGatewayRemoteDirectories,
  listGatewaySshConnections,
} from "../../gatewayApi";
import type {
  AddSshGatewayWorkspaceRequest,
  GatewayDirectoryList,
  SshConnectionOption,
} from "../../types/backend";
import {
  buildSshWorkspaceRequest,
  INITIAL_SSH_WORKSPACE_FORM,
  type WorkspaceSshFormState,
} from "../../utils/workspaceSshRequest";
import WorkspaceDirectoryBrowser from "./WorkspaceDirectoryBrowser";

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
  const [browserListing, setBrowserListing] = useState<GatewayDirectoryList | null>(null);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);
  const selectedConnection = useMemo(
    () => connections.find((connection) => connection.connection_id === connectionId),
    [connectionId, connections],
  );

  const update = (key: keyof WorkspaceSshFormState, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const loadDirectory = useCallback(
    async (targetConnectionId: string, path: string | null) => {
      setBrowserLoading(true);
      setBrowserError(null);
      try {
        const listing = await browseGatewayRemoteDirectories(
          apiPort,
          targetConnectionId,
          path,
        );
        setBrowserListing(listing);
        setForm((current) => ({
          ...current,
          remoteWorkspacePath: listing.path,
        }));
      } catch (loadError) {
        setBrowserError(
          loadError instanceof Error ? loadError.message : String(loadError),
        );
      } finally {
        setBrowserLoading(false);
      }
    },
    [apiPort],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    setError(null);
    setSubmitting(false);
    setConnectionsLoading(true);
    setBrowserError(null);
    void listGatewaySshConnections(apiPort)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setConnections(result.items);
        const initialConnection = result.items[0];
        if (!initialConnection) {
          setConnectionId("");
          setBrowserListing(null);
          setError(
            "没有可用的 SSH Host。请先在 ~/.ssh/config 中添加具体 Host，或在 BoxTeam 配置中注册 SSH 工作区。",
          );
          return;
        }
        setConnectionId(initialConnection.connection_id);
        const initialPath = initialConnection.initial_path ?? "";
        setForm((current) => ({
          ...current,
          remoteWorkspacePath: initialPath,
        }));
        void loadDirectory(initialConnection.connection_id, initialPath || null);
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
  }, [apiPort, loadDirectory, open]);

  if (!open) {
    return null;
  }

  const handleConnectionChange = (nextConnectionId: string) => {
    setConnectionId(nextConnectionId);
    setBrowserListing(null);
    setBrowserError(null);
    const connection = connections.find(
      (candidate) => candidate.connection_id === nextConnectionId,
    );
    const initialPath = connection?.initial_path ?? "";
    update("remoteWorkspacePath", initialPath);
    void loadDirectory(nextConnectionId, initialPath || null);
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
            <h2 id="workspace-ssh-dialog-title">添加远程工作区</h2>
            <p>选择已配置的远程主机，然后浏览并选择项目目录。</p>
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
            <span>远程主机</span>
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
              <small>SSH 主机来源：BoxTeam 配置与当前用户 ~/.ssh/config。</small>
            )}
          </label>

          <label className="workspace-dialog-wide">
            <span>文件夹路径</span>
            <span className="workspace-path-input-shell">
              <span className="codicon codicon-folder" aria-hidden="true" />
              <input
                value={form.remoteWorkspacePath}
                onChange={(event) => update("remoteWorkspacePath", event.target.value)}
                placeholder="选择目录或输入绝对路径"
              />
            </span>
          </label>

          {selectedConnection ? (
            <WorkspaceDirectoryBrowser
              listing={browserListing}
              currentPath={form.remoteWorkspacePath}
              loading={browserLoading}
              error={browserError}
              onNavigate={(path) => void loadDirectory(connectionId, path)}
            />
          ) : null}

          <label>
            <span>工作区名称</span>
            <input
              value={form.name}
              onChange={(event) => update("name", event.target.value)}
              placeholder="可留空"
            />
          </label>
        </div>
        {error ? <div className="workspace-dialog-error">{error}</div> : null}
        <div className="workspace-remote-dialog-hint">
          选择的目录需要有可访问的 BoxTeam 工作区后端；连接失败会直接显示详细错误。
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
            {submitting ? "添加中" : "添加项目"}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
